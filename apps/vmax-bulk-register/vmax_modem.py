# -*- coding: utf-8 -*-
"""vmax_modem - read verification SMS straight off a real SIM (AT + PDU).

Why
---
`vmax_smsd` can already accept codes over HTTP, but that still needs a phone app
forwarding each SMS.  A USB dongle / EC20 / ML307 / SIM800 with the card in it is
the most dependable receive side: no cloud, no app, no battery, no screen.  This
module turns such a modem into the same inbox stream:

    SIM --AT--> vmax_modem --PDU--> vmax_smsd Store --HTTP--> vmax_register

Usage
-----
    python vmax_smsd.py --port 8787 --secret <TEST_SECRET> --modem COM3
    python vmax_smsd.py --port 8787 --secret <TEST_SECRET> --modem tcp://192.168.1.50:2000
    python vmax_modem.py --probe COM3       # one-shot: model / SIM / signal / number
    python vmax_modem.py --self-test        # decode built-in PDUs, no hardware

`virtual` is a scripted modem used by tests and demos:

    python vmax_smsd.py --modem virtual --port 8787 --secret <TEST_SECRET>
    curl -X POST "http://127.0.0.1:8787/virtual/sms?secret=<TEST_SECRET>" \
         -d "phone=13800138000&text=【VobileMax】您的验证码为 482913"

Serial needs pyserial (`pip install pyserial`); `tcp://` needs nothing and works
with ser2net / ESP-Link / a modem that exposes its own TCP bridge.

PDU coverage: SMS-DELIVER, GSM 7-bit (incl. the escape table), UCS2, 8-bit data,
alphanumeric senders, UDH concatenation (8- and 16-bit references) - i.e. what
Chinese operators and gateways actually send.

Design notes
------------
* `Transport` is duck-typed: `.write(bytes)`, `.readline(timeout) -> bytes`,
  `.close()`.  Tests (and `--modem virtual`) use an in-memory modem that speaks
  the same AT dialect, so the whole parser is exercised without hardware.
* Reconnect is automatic and bounded: any exception closes the transport, backs
  off 1s -> 30s, and re-runs the handshake.  A yanked USB cable must not end the
  batch run.
* Startup sweeps `AT+CMGL` so codes that arrived while the process was down are
  still picked up; after ingest the slot is deleted (unless --modem-keep).
* Duplicate protection: the same (phone, text, scts) seen twice - e.g. sweep and
  a late +CMTI for the same slot - is emitted once.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
import time
from collections import OrderedDict

try:  # keep `--help` working on machines without the shared console helper
    from vmax_console import utf8_stdout
except Exception:  # pragma: no cover
    def utf8_stdout():
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

utf8_stdout()

# --------------------------------------------------------------------------- #
# GSM 03.38 alphabet
# --------------------------------------------------------------------------- #
GSM7 = (
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\u001b\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
)
assert len(GSM7) == 128, len(GSM7)

GSM7_EXT = {
    0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\",
    0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "\u20ac",
}
GSM7_INDEX = {ch: i for i, ch in enumerate(GSM7) if i != 0x1B}
GSM7_EXT_REV = {ch: code for code, ch in GSM7_EXT.items()}

class ModemError(RuntimeError):
    pass


class ModemTimeout(ModemError):
    pass

# --------------------------------------------------------------------------- #
# PDU codec
# --------------------------------------------------------------------------- #
def _swap_nibbles(raw: bytes) -> str:
    out = []
    for byte in raw:
        out.append("%X%X" % (byte & 0x0F, byte >> 4))
    return "".join(out)


def decode_address(raw: bytes, length: int, toa: int) -> str:
    """TP-OA: international / national / alphanumeric."""
    ton = (toa >> 4) & 0x07
    if ton == 5:  # alphanumeric: packed 7-bit, length counts semi-octets
        septets = (length * 4) // 7
        return unpack_septets(raw, septets).rstrip("@")
    digits = _swap_nibbles(raw)[:length].rstrip("F")
    return ("+" if ton == 1 else "") + digits


def decode_scts(raw: bytes) -> str:
    digits = _swap_nibbles(raw)
    yy, mo, dd, hh, mi, ss = (digits[i:i + 2] for i in range(0, 12, 2))
    tz = digits[12:14]
    if tz:
        sign = "-" if tz[0] in "89" else "+"
        try:
            quarters = int(tz)
        except ValueError:
            quarters = 0
        tz = "%s%02d:%02d" % (sign, quarters // 4, (quarters % 4) * 15)
    return "20%s-%s-%sT%s:%s:%s%s" % (yy, mo, dd, hh, mi, ss, tz)


def unpack_septets(data: bytes, count: int) -> str:
    """Unpack *count* septets (escape sequences count as two)."""
    val = 0
    bits = 0
    out = []
    esc = False
    used = 0
    for byte in data:
        val |= byte << bits
        bits += 8
        while bits >= 7 and used < count:
            code = val & 0x7F
            val >>= 7
            bits -= 7
            used += 1
            if esc:
                out.append(GSM7_EXT.get(code, GSM7[code]))
                esc = False
            elif code == 0x1B:
                esc = True
            else:
                out.append(GSM7[code])
    return "".join(out)


def pack_septets(text: str) -> bytes:
    vals = []
    for ch in text:
        if ch in GSM7_EXT_REV:
            vals.extend((0x1B, GSM7_EXT_REV[ch]))
        elif ch in GSM7_INDEX:
            vals.append(GSM7_INDEX[ch])
        else:
            raise ValueError("not GSM 7-bit: %r" % ch)
    acc = 0
    bits = 0
    out = bytearray()
    for value in vals:
        acc |= value << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def _parse_udh(udh: bytes) -> dict:
    info = {}
    pos = 0
    while pos + 1 < len(udh):
        iei = udh[pos]
        ln = udh[pos + 1]
        val = udh[pos + 2:pos + 2 + ln]
        pos += 2 + ln
        if len(val) < ln:
            break
        if iei == 0x00 and ln == 3:
            info.update(ref=val[0], total=val[1], seq=val[2])
        elif iei == 0x08 and ln == 4:
            info.update(ref=(val[0] << 8) | val[1], total=val[2], seq=val[3])
        elif iei == 0x24 and ln == 2:  # port addressing (WAP push etc.)
            info["port"] = (val[0] << 8) | val[1]
    return info


def decode_pdu(pdu: str) -> dict:
    """Decode one SMS-DELIVER PDU (hex string) into a dict."""
    hexstr = re.sub(r"\s+", "", pdu or "").upper()
    if len(hexstr) % 2:
        raise ValueError("odd-length PDU")
    raw = bytes.fromhex(hexstr)
    if len(raw) < 12:
        raise ValueError("PDU too short")
    pos = 0
    smsc_len = raw[pos]
    pos += 1 + smsc_len
    if pos >= len(raw):
        raise ValueError("truncated PDU (SMSC)")
    fo = raw[pos]
    pos += 1
    mti = fo & 0x03
    if mti != 0:
        raise ValueError("not SMS-DELIVER (TP-MTI=%d)" % mti)
    udhi = bool(fo & 0x40)
    oa_len = raw[pos]
    pos += 1
    toa = raw[pos]
    pos += 1
    addr_bytes = (oa_len + 1) // 2
    phone = decode_address(raw[pos:pos + addr_bytes], oa_len, toa)
    pos += addr_bytes
    pid = raw[pos]
    pos += 1
    dcs = raw[pos]
    pos += 1
    scts = decode_scts(raw[pos:pos + 7]) if len(raw) >= pos + 7 else ""
    pos += 7
    udl = raw[pos]
    pos += 1
    alp = (dcs >> 2) & 0x03
    if alp == 0:
        ud = raw[pos:pos + (udl * 7 + 7) // 8]
    else:
        ud = raw[pos:pos + udl]

    udh = None
    if udhi and ud:
        udhl = ud[0]
        udh = _parse_udh(ud[1:1 + udhl])
        if alp == 0:
            skip = ((udhl + 1) * 8 + 6) // 7
            text = unpack_septets(ud[1 + udhl:], max(0, udl - skip))
        else:
            text = ud[1 + udhl:]
    else:
        if alp == 0:
            text = unpack_septets(ud, udl)
        else:
            text = ud

    if alp == 2:
        out_text = bytes(text).decode("utf-16-be", "replace")
    elif alp == 0:
        out_text = text if isinstance(text, str) else bytes(text).decode("latin-1")
    else:
        out_text = bytes(text).decode("latin-1").rstrip("\x00")

    return {
        "phone": phone,
        "text": out_text,
        "scts": scts,
        "dcs": dcs,
        "pid": pid,
        "udh": udh,
        "encoding": {0: "gsm7", 1: "8bit", 2: "ucs2"}.get(alp, "?"),
    }


def _encode_address(phone: str) -> bytes:
    digits = phone.lstrip("+")
    if not re.fullmatch(r"[0-9]{3,20}", digits):
        raise ValueError("bad address %r" % phone)
    toa = 0x91 if phone.startswith("+") else 0x81
    if len(digits) % 2:
        digits += "F"
    packed = bytearray()
    for i in range(0, len(digits), 2):
        lo = int(digits[i], 16)
        hi = int(digits[i + 1], 16)
        packed.append(lo | (hi << 4))
    return bytes([len(phone.lstrip("+")), toa]) + bytes(packed)


def _encode_septet_address(text: str) -> bytes:
    packed = pack_septets(text)
    return bytes([len(packed) * 2, 0xD0]) + packed


def _scts_now() -> str:
    t = time.localtime()
    return "%02d%02d%02d%02d%02d%02d00" % (
        t.tm_year % 100, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


def _append_scts(out: bytearray, digits: str) -> None:
    """TP-SCTS is nibble-swapped like a phone number."""
    for i in range(0, 14, 2):
        out.append(int(digits[i]) | (int(digits[i + 1]) << 4))


def build_deliver_pdu(phone: str, text: str, *, scts: str | None = None,
                      ref: int | None = None, total: int = 1, seq: int = 1,
                      alpha: str | None = None) -> str:
    """Build an SMS-DELIVER PDU.  Used by the virtual modem and by tests.

    Concatenated parts are always UCS2 so that the body starts on an octet
    boundary (no septet realignment needed).
    """
    out = bytearray(b"\x00")  # SMSC: none
    udh = b""
    if ref is not None:
        udh = bytes([0x05, 0x00, 0x03, ref & 0xFF, total & 0xFF, seq & 0xFF])
    out.append(0x04 | (0x40 if udh else 0x00))
    if alpha:
        out += _encode_septet_address(alpha)
    else:
        out += _encode_address(phone)
    out.append(0x00)  # TP-PID
    digits = (scts or _scts_now())

    if udh:
        out.append(0x08)  # UCS2
        _append_scts(out, digits)
        ud = udh + text.encode("utf-16-be")
        out.append(len(ud))
        out += ud
        return out.hex().upper()

    try:
        packed = pack_septets(text)
        out.append(0x00)  # GSM 7-bit
        _append_scts(out, digits)
        septets = sum(2 if ch in GSM7_EXT_REV else 1 for ch in text)
        out.append(septets)
        out += packed
        return out.hex().upper()
    except ValueError:
        body = text.encode("utf-16-be")
        out.append(0x08)
        _append_scts(out, digits)
        out.append(len(body))
        out += body
        return out.hex().upper()


def normalize_phone(value: str) -> str:
    """'+8613800138000' / '8613800138000' / '138 0013 8000' -> '13800138000'."""
    digits = re.sub(r"[^0-9]", "", value or "")
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits

# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
class Transport:
    def write(self, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def readline(self, timeout: float) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.2):
        try:
            import serial  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on host
            raise ModemError(
                "pyserial is not installed: pip install pyserial   "
                "(or use --modem tcp://host:port with ser2net/ESP-Link)") from exc
        self.port = port
        self.ser = serial.Serial(port, baudrate=baud, timeout=timeout,
                                 write_timeout=5, exclusive=True)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def readline(self, timeout: float) -> bytes:
        self.ser.timeout = max(0.01, timeout)
        return self.ser.readline()

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class TcpTransport(Transport):
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def readline(self, timeout: float) -> bytes:
        deadline = time.time() + max(0.01, timeout)
        while b"\n" not in self.buf:
            remain = deadline - time.time()
            if remain <= 0:
                return b""
            self.sock.settimeout(remain)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return b""
            if not chunk:
                raise ModemError("tcp modem closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line + b"\n"

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class VirtualModemTransport(Transport):
    """Scripted modem: same AT dialect, no hardware.

    `messages` pre-loads the SIM storage (arrived while the reader was down);
    `inject()` simulates a new SMS and emits +CMTI / +CMT according to CNMI.
    """

    MODEL = "vmax virtual modem (Quectel EC20 compatible)"

    def __init__(self, messages=None, number: str = "+8613800138000",
                 signal: int = 24, operator: str = "CHINA MOBILE"):
        self.lock = threading.Lock()
        self.out = []
        self.msgs = []          # list of dicts {phone,text,pdu,alpha}
        self.number = number
        self.signal = signal
        self.operator = operator
        self.cmgf = 1
        self.cnmi_mt = 0
        self.deleted = []
        self.commands = []
        self.closed = False
        for phone, text in (messages or []):
            self._store(phone, text)
        self.injected = 0

    # -- virtual hardware -------------------------------------------------
    def _store(self, phone: str, text: str, alpha: str | None = None) -> int:
        pdu = build_deliver_pdu(phone, text, alpha=alpha)
        self.msgs.append({"phone": phone, "text": text, "pdu": pdu, "alpha": alpha})
        return len(self.msgs)

    def inject(self, phone: str, text: str, alpha: str | None = None) -> int:
        with self.lock:
            idx = self._store(phone, text, alpha)
            self.injected += 1
            if self.cnmi_mt == 2:
                pdu = self.msgs[idx - 1]["pdu"]
                self.out.append("+CMT: ,%d" % (len(pdu) // 2))
                self.out.append(pdu)
            elif self.cnmi_mt == 1:
                self.out.append('+CMTI: "ME",%d' % idx)
            return idx

    def _text_body(self, phone: str, text: str) -> list[str]:
        return ['+CMGR: "REC UNREAD","%s",,"%s"'
                % (phone, time.strftime("%y/%m/%d,%H:%M:%S+32")), text]

    def _handle(self, cmd: str) -> list[str]:
        u = cmd.strip()
        up = u.upper()
        self.commands.append(u)
        if up in ("AT", "ATZ", "ATE0", "ATE1", "AT&F"):
            return ["OK"]
        if up.startswith("AT+CMGF?"):
            return ["+CMGF: %d" % self.cmgf, "OK"]
        if up.startswith("AT+CMGF="):
            self.cmgf = int(u.split("=", 1)[1])
            return ["OK"]
        if up.startswith("AT+CPMS=") or up.startswith("AT+CPMS?"):
            return ["+CPMS: 0,50,0,50,0,50", "OK"]
        if up.startswith("AT+CNMI="):
            parts = u.split("=", 1)[1].split(",")
            try:
                self.cnmi_mt = int(parts[1])
            except (IndexError, ValueError):
                self.cnmi_mt = 0
            return ["OK"]
        if up.startswith("AT+CSDH"):
            return ["OK"]
        if up.startswith("AT+CMGD="):
            idx = int(u.split("=", 1)[1].split(",")[0])
            self.deleted.append(idx)
            if 1 <= idx <= len(self.msgs):
                self.msgs[idx - 1] = None
            return ["OK"]
        if up.startswith("AT+CMGR="):
            idx = int(u.split("=", 1)[1].split(",")[0])
            if not (1 <= idx <= len(self.msgs)) or self.msgs[idx - 1] is None:
                return ["+CMS ERROR: 321"]
            msg = self.msgs[idx - 1]
            if self.cmgf == 0:
                return ["+CMGR: 0,,%d" % (len(msg["pdu"]) // 2), msg["pdu"], "OK"]
            return self._text_body(msg["phone"], msg["text"]) + ["OK"]
        if up.startswith("AT+CMGL"):
            lines = []
            for idx, msg in enumerate(self.msgs, 1):
                if msg is None:
                    continue
                if self.cmgf == 0:
                    lines += ["+CMGL: %d,0,,%d" % (idx, len(msg["pdu"]) // 2), msg["pdu"]]
                else:
                    lines += ['+CMGL: %d,"REC UNREAD","%s",,"%s"'
                              % (idx, msg["phone"], time.strftime("%y/%m/%d,%H:%M:%S+32")),
                              msg["text"]]
            return lines + ["OK"]
        if up.startswith("AT+CPIN?"):
            return ["+CPIN: READY", "OK"]
        if up.startswith("AT+CSQ"):
            return ["+CSQ: %d,99" % self.signal, "OK"]
        if up.startswith("AT+COPS?"):
            return ['+COPS: 0,0,"%s",7' % self.operator, "OK"]
        if up.startswith("AT+CREG?"):
            return ["+CREG: 0,1", "OK"]
        if up.startswith("AT+CNUM"):
            return ['+CNUM: ,"%s",145' % self.number, "OK"]
        if up.startswith("AT+CGSN"):
            return ["866425030000001", "OK"]
        if up.startswith("ATI"):
            return [self.MODEL, "OK"]
        if up.startswith("AT+CGDCONT"):
            return ["OK"]
        return ["ERROR"]

    # -- Transport --------------------------------------------------------
    def write(self, data: bytes) -> None:
        if self.closed:
            raise ModemError("virtual modem closed")
        cmd = data.decode("ascii", "replace").strip("\r\n\x00")
        if not cmd:
            return
        with self.lock:
            self.out.extend(self._handle(cmd))

    def readline(self, timeout: float) -> bytes:
        deadline = time.time() + max(0.01, timeout)
        while True:
            with self.lock:
                if self.out:
                    return (self.out.pop(0) + "\n").encode("utf-8")
            if self.closed:
                return b""
            if time.time() >= deadline:
                return b""
            time.sleep(0.01)

    def close(self) -> None:
        self.closed = True


def parse_modem_spec(spec: str, baud: int = 115200) -> Transport:
    s = (spec or "").strip()
    low = s.lower()
    if low in ("virtual", "demo", "loopback"):
        return VirtualModemTransport()
    if low.startswith("tcp://"):
        hostport = s[6:]
        if ":" not in hostport:
            raise ModemError("tcp modem needs tcp://host:port")
        host, port = hostport.rsplit(":", 1)
        return TcpTransport(host, int(port))
    if low.startswith("serial://"):
        s = s[9:]
    return SerialTransport(s, baud=baud)

# --------------------------------------------------------------------------- #
# AT driver
# --------------------------------------------------------------------------- #
class AtModem:
    def __init__(self, factory, on_message, *, name: str = "modem",
                 delete: bool = True, pdu_mode: bool = True,
                 timeout: float = 3.0, reconnect_min: float = 1.0,
                 reconnect_max: float = 30.0, keepalive: float = 30.0,
                 log=print):
        self.factory = factory
        self.on_message = on_message
        self.name = name
        self.delete = delete
        self.pdu_mode = pdu_mode
        self.timeout = timeout
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.keepalive = keepalive
        self.log = log
        self.transport: Transport | None = None
        self.stats = {"received": 0, "errors": 0, "reconnects": 0,
                      "duplicates": 0, "last_error": None, "connected": False}
        self._seen: OrderedDict = OrderedDict()
        self._concat: dict = {}

    # -- low level --------------------------------------------------------
    def _write(self, text: str) -> None:
        if self.transport is None:
            raise ModemError("not connected")
        self.transport.write((text + "\r").encode("ascii", "replace"))

    def cmd(self, line: str, timeout: float | None = None) -> list[str]:
        self._write(line)
        deadline = time.time() + (timeout or self.timeout)
        out = []
        while time.time() < deadline:
            raw = self.transport.readline(max(0.05, deadline - time.time()))
            if not raw:
                continue
            text = raw.decode("utf-8", "replace").strip("\r\n\x00 ")
            if not text:
                continue
            if text == line.strip():
                continue
            out.append(text)
            up = text.upper()
            if up == "OK":
                return out
            if up.startswith("ERROR") or up.startswith("+CME ERROR") or up.startswith("+CMS ERROR"):
                raise ModemError(text)
        raise ModemTimeout("no answer to %r after %.1fs" % (line, timeout or self.timeout))

    def connect(self) -> None:
        self.transport = self.factory()
        self.stats["connected"] = True
        try:
            self.cmd("AT", timeout=5)
            try:
                self.cmd("ATE0", timeout=3)
            except ModemError:
                pass
            self._check_sim()
            self.cmd("AT+CMGF=%d" % (0 if self.pdu_mode else 1), timeout=3)
            try:
                self.cmd('AT+CPMS="ME","ME","ME"', timeout=3)
            except ModemError:
                pass
            try:
                self.cmd("AT+CNMI=2,1,0,0,0", timeout=3)
            except ModemError:
                pass
            if self.pdu_mode:
                try:
                    self.cmd("AT+CSDH=0", timeout=3)
                except ModemError:
                    pass
        except Exception:
            self.close()
            raise

    def _check_sim(self) -> None:
        """A PIN-locked or missing SIM must fail loudly, not silently receive nothing."""
        try:
            lines = self.cmd("AT+CPIN?", timeout=5)
        except ModemError:
            return
        text = " ".join(lines)
        if "READY" not in text.upper():
            raise ModemError("SIM not ready: %s (unlock the PIN or reseat the card)" % text)

    def close(self) -> None:
        self.stats["connected"] = False
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
            self.transport = None

    # -- parsing ----------------------------------------------------------
    def _parse_cmgr(self, lines: list[str]) -> dict | None:
        header = next((l for l in lines if l.startswith("+CMGR:")), None)
        if header is None:
            return None
        if self.pdu_mode:
            pdu = next((l for l in reversed(lines)
                        if re.fullmatch(r"[0-9A-Fa-f]{20,}", l.strip())), None)
            if not pdu:
                return None
            return decode_pdu(pdu)
        body = "\n".join(lines[lines.index(header) + 1:]).strip()
        phone = ""
        m = re.match(r'\+CMGR:\s*"[^"]*","([^"]*)"', header)
        if m:
            phone = m.group(1)
        return {"phone": phone, "text": body, "scts": "", "dcs": None,
                "udh": None, "encoding": "text"}

    def _parse_cmgl(self, lines: list[str]) -> list[tuple[int, dict]]:
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.startswith("+CMGL:"):
                i += 1
                continue
            idx_m = re.search(r"\+CMGL:\s*(\d+)", line)
            idx = int(idx_m.group(1)) if idx_m else 0
            if self.pdu_mode:
                pdu = None
                j = i + 1
                while j < len(lines) and not lines[j].startswith("+CMGL:"):
                    if re.fullmatch(r"[0-9A-Fa-f]{20,}", lines[j].strip()):
                        pdu = lines[j].strip()
                        break
                    j += 1
                if pdu:
                    try:
                        out.append((idx, decode_pdu(pdu)))
                    except Exception as exc:
                        self.stats["errors"] += 1
                        self.stats["last_error"] = "decode: %s" % exc
                i = j + 1 if pdu else i + 1
            else:
                body = lines[i + 1] if i + 1 < len(lines) else ""
                phone = ""
                m = re.match(r'\+CMGL:\s*\d+,\s*"[^"]*",\s*"([^"]*)"', line)
                if m:
                    phone = m.group(1)
                out.append((idx, {"phone": phone, "text": body, "scts": "",
                                  "dcs": None, "udh": None, "encoding": "text"}))
                i += 2
        return out

    def _emit(self, msg: dict) -> None:
        phone = normalize_phone(msg.get("phone") or "")
        text = (msg.get("text") or "").strip()
        if not phone or not text:
            return
        udh = msg.get("udh") or {}
        ref, total, seq = udh.get("ref"), udh.get("total"), udh.get("seq")
        if ref is not None and total and total > 1 and seq:
            key = (phone, ref, total)
            entry = self._concat.setdefault(key, {"parts": {}, "ts": time.time()})
            entry["parts"][seq] = text
            entry["ts"] = time.time()
            if len(entry["parts"]) < total:
                return
            text = "".join(entry["parts"][i] for i in sorted(entry["parts"]))
            self._concat.pop(key, None)
            msg = dict(msg, text=text, concat=total)
        seen_key = (phone, text, msg.get("scts") or "")
        if seen_key in self._seen:
            self.stats["duplicates"] += 1
            return
        self._seen[seen_key] = time.time()
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        self.stats["received"] += 1
        out = dict(msg, phone=phone, text=text, source=self.name)
        self.on_message(out)

    def _prune_concat(self) -> None:
        cutoff = time.time() - 120
        for key in [k for k, v in self._concat.items() if v["ts"] < cutoff]:
            self._concat.pop(key, None)

    def read_index(self, idx: int) -> int:
        try:
            lines = self.cmd("AT+CMGR=%d" % idx)
        except ModemError as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = str(exc)
            return 0
        msg = self._parse_cmgr(lines)
        if msg is None:
            return 0
        self._emit(msg)
        if self.delete:
            try:
                self.cmd("AT+CMGD=%d" % idx, timeout=5)
            except ModemError as exc:
                self.stats["last_error"] = "delete: %s" % exc
        return 1

    def sweep(self, fatal: bool = False) -> int:
        try:
            lines = self.cmd('AT+CMGL=4' if self.pdu_mode else 'AT+CMGL="ALL"', timeout=15)
        except ModemError as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = "sweep: %s" % exc
            if fatal:
                raise
            return 0
        n = 0
        for idx, msg in self._parse_cmgl(lines):
            self._emit(msg)
            n += 1
            if self.delete and idx:
                try:
                    self.cmd("AT+CMGD=%d" % idx, timeout=5)
                except ModemError:
                    pass
        if n:
            self.log("[modem %s] swept %d stored message(s)" % (self.name, n))
        return n

    # -- main loop --------------------------------------------------------
    def _listen(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        last_ping = time.time()
        while not stop.is_set():
            raw = self.transport.readline(1.0)
            if raw:
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                m = re.match(r'\+CMTI:\s*"[^"]*",\s*(\d+)', text)
                if m:
                    self.read_index(int(m.group(1)))
                    continue
                if text.startswith("+CMT:"):
                    body = self.transport.readline(2.0).decode("utf-8", "replace").strip()
                    if self.pdu_mode and re.fullmatch(r"[0-9A-Fa-f]{20,}", body):
                        try:
                            self._emit(decode_pdu(body))
                        except Exception as exc:
                            self.stats["errors"] += 1
                            self.stats["last_error"] = "decode: %s" % exc
                    elif body:
                        self._emit({"phone": "", "text": body, "scts": ""})
                    continue
                continue
            if time.time() - last_ping > self.keepalive:
                self.cmd("AT", timeout=3)
                last_ping = time.time()
            self._prune_concat()

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        backoff = self.reconnect_min
        while not stop.is_set():
            try:
                self.connect()
                backoff = self.reconnect_min
                self.sweep(fatal=True)   # a dead transport must reach the reconnect path
                self._listen(stop)
            except Exception as exc:
                self.stats["errors"] += 1
                self.stats["last_error"] = "%s: %s" % (type(exc).__name__, exc)
                self.close()
                if stop.is_set():
                    break
                self.stats["reconnects"] += 1
                self.log("[modem %s] %s -> retry in %.0fs" % (self.name, exc, backoff))
                stop.wait(backoff)
                backoff = min(self.reconnect_max, backoff * 2)

    def start(self, name: str | None = None) -> threading.Thread:
        t = threading.Thread(target=self.run, name=name or ("modem-" + self.name),
                             daemon=True)
        t.start()
        return t

    # -- diagnostics ------------------------------------------------------
    def probe(self) -> dict:
        self.connect()
        info = {"modem": self.name}
        for key, cmd in (("model", "ATI"), ("sim", "AT+CPIN?"), ("signal", "AT+CSQ"),
                         ("operator", "AT+COPS?"), ("number", "AT+CNUM"),
                         ("imei", "AT+CGSN"), ("reg", "AT+CREG?"),
                         ("pdu_mode", "AT+CMGF?"), ("storage", "AT+CPMS?")):
            try:
                info[key] = " | ".join(self.cmd(cmd, timeout=5))
            except Exception as exc:
                info[key] = "ERR %s" % exc
        return info

# --------------------------------------------------------------------------- #
# self-test (no hardware)
# --------------------------------------------------------------------------- #
KNOWN_GSM7 = ("07911326040000F0040B911346610089F6000020806291731408"
              "0CC8F71D14969741F977FD07")

SELF_PDUS = [
    (KNOWN_GSM7, "+31641600986", "How are you?"),
]


def self_test() -> int:
    bad = 0
    print("[1] GSM 7-bit reference PDU (GSM 03.38 example)")
    for pdu, want_phone, want_text in SELF_PDUS:
        got = decode_pdu(pdu)
        ok = got["phone"] == want_phone and got["text"] == want_text
        bad += 0 if ok else 1
        print("  %s phone=%r text=%r (want %r / %r)"
              % ("PASS" if ok else "FAIL", got["phone"], got["text"],
                 want_phone, want_text))

    print("[2] UCS2 Chinese body (typical operator SMS)")
    text = "【VobileMax】您的验证码为 482913，5 分钟内有效。"
    pdu = build_deliver_pdu("+8613800138000", text, scts="26102609412300")
    got = decode_pdu(pdu)
    ok = got["phone"] == "+8613800138000" and got["text"] == text and got["encoding"] == "ucs2"
    bad += 0 if ok else 1
    print("  %s %r -> %r (%s)" % ("PASS" if ok else "FAIL", text[:16], got["text"][:16],
                                  got["encoding"]))

    print("[3] alphanumeric sender")
    pdu = build_deliver_pdu("", "Your code: 660021", alpha="VobileMax")
    got = decode_pdu(pdu)
    ok = got["phone"] == "VobileMax" and got["text"] == "Your code: 660021"
    bad += 0 if ok else 1
    print("  %s sender=%r text=%r" % ("PASS" if ok else "FAIL", got["phone"], got["text"]))

    print("[4] concatenated UCS2 (UDH 8-bit reference)")
    part1 = "【VobileMax】您的验证码为 482913"
    part2 = "，5 分钟内有效。"
    a = decode_pdu(build_deliver_pdu("+8613800138000", part1, ref=7, total=2, seq=1))
    b = decode_pdu(build_deliver_pdu("+8613800138000", part2, ref=7, total=2, seq=2))
    ok = (a["udh"] or {}).get("total") == 2 and a["text"] == part1 and b["text"] == part2
    bad += 0 if ok else 1
    print("  %s udh=%r text=%r" % ("PASS" if ok else "FAIL", a["udh"], a["text"]))

    print("[5] phone normalisation")
    cases = [("+8613800138000", "13800138000"), ("86 138 0013 8000", "13800138000"),
             ("13800138000", "13800138000"), ("+31641600986", "31641600986")]
    for src, want in cases:
        got = normalize_phone(src)
        ok = got == want
        bad += 0 if ok else 1
        print("  %s %-20r -> %r" % ("PASS" if ok else "FAIL", src, got))

    print("[6] GSM 7-bit round trip (incl. escape table)")
    text = "Price 5\u20ac {ok} [x] ~ | \\ ^"
    got = decode_pdu(build_deliver_pdu("+8613800138000", text))
    ok = got["text"] == text
    bad += 0 if ok else 1
    print("  %s %r -> %r" % ("PASS" if ok else "FAIL", text, got["text"]))

    print("self-test:", "PASS" if not bad else "%d FAILED" % bad)
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AT/PDU SMS reader for vmax_smsd")
    ap.add_argument("--probe", metavar="MODEM",
                    help="serial port (COM3), serial://COM3 or tcp://host:port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--mode", choices=("pdu", "text"), default="pdu")
    ap.add_argument("--once", action="store_true", help="sweep stored SMS and exit")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)
    if ns.self_test:
        return self_test()
    if not ns.probe:
        ap.print_help()
        return 2
    got = []
    modem = AtModem(lambda: parse_modem_spec(ns.probe, ns.baud),
                    lambda m: got.append(m), name=ns.probe,
                    pdu_mode=(ns.mode == "pdu"), delete=False)
    try:
        info = modem.probe()
    except Exception as exc:
        print("[probe] FAILED: %s" % exc, flush=True)
        return 1
    if ns.json:
        print(json.dumps({"info": info, "messages": got}, ensure_ascii=False, indent=1))
    else:
        for k, v in info.items():
            print("%-10s %s" % (k, v))
    if ns.once:
        try:
            n = modem.sweep()
            print("swept %d message(s)" % n)
        except Exception as exc:
            print("[once] sweep failed: %s" % exc, flush=True)
    modem.close()
    if got:
        print("\n%d stored message(s):" % len(got))
        for m in got:
            print("  %-16s %r" % (m["phone"], m["text"][:60]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
