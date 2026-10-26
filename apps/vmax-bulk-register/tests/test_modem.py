# -*- coding: utf-8 -*-
"""Offline checks for vmax_modem + vmax_smsd --modem (no hardware, no network beyond loopback).

Everything runs against VirtualModemTransport, which speaks the same AT dialect
as a real EC20/SIM800: handshake, CNMI indicators, CMGR/CMGL/CMGD, PDU bodies.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vmax_modem as M  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " | %s" % (detail,)))
    if not cond:
        fails.append(name)


def pdu(phone, text, **kw):
    return M.build_deliver_pdu(phone, text, **kw)


def main():
    print("[1] GSM 7-bit reference PDU")
    got = M.decode_pdu(M.KNOWN_GSM7)
    check("sender decoded", got["phone"] == "+31641600986", got["phone"])
    check("text decoded", got["text"] == "How are you?", got["text"])
    check("encoding is gsm7", got["encoding"] == "gsm7", got["encoding"])

    print("[2] UCS2 body + SCTS")
    body = "\u3010VobileMax\u3011\u60a8\u7684\u9a8c\u8bc1\u7801\u4e3a 482913\uff0c5 \u5206\u949f\u5185\u6709\u6548\u3002"
    got = M.decode_pdu(pdu("+8613800138000", body, scts="26102609412332"))
    check("phone", got["phone"] == "+8613800138000", got["phone"])
    check("body", got["text"] == body, got["text"])
    check("dcs 0x08", got["dcs"] == 0x08, got["dcs"])
    check("encoding ucs2", got["encoding"] == "ucs2", got["encoding"])
    check("scts +08:00", got["scts"] == "2026-10-26T09:41:23+08:00", got["scts"])

    print("[3] alphanumeric sender")
    got = M.decode_pdu(pdu("", "Your code: 660021", alpha="VobileMax"))
    check("sender is text", got["phone"] == "VobileMax", got["phone"])
    check("body", got["text"] == "Your code: 660021", got["text"])

    print("[4] GSM 7-bit escapes round trip")
    for label, text in (("euro sign", "Price 5\u20ac"),
                        ("escapes", "{a} [b] ~ | \\ ^"),
                        ("plain ascii", "plain ascii 123")):
        got = M.decode_pdu(pdu("+8613800138000", text))
        check("round trip (%s)" % label, got["text"] == text, got["text"])

    print("[5] UDH concatenation")
    a = M.decode_pdu(pdu("+8613800138000", "part-one ", ref=7, total=2, seq=1))
    b = M.decode_pdu(pdu("+8613800138000", "part-two", ref=7, total=2, seq=2))
    check("udh ref/total/seq", a["udh"] == {"ref": 7, "total": 2, "seq": 1}, a["udh"])
    check("part 1 body", a["text"] == "part-one ", a["text"])
    check("part 2 body", b["text"] == "part-two", b["text"])

    print("[6] 8-bit data PDU and rejects")
    eight = "00040B911346610089F60004" + "62106290413200" + "03" + "414243"
    got = M.decode_pdu(eight)
    check("8bit text", got["text"] == "ABC", got["text"])
    check("8bit encoding", got["encoding"] == "8bit", got["encoding"])
    for bad, why in (("00", "short"), ("0001" + "00" * 20, "submit")):
        try:
            M.decode_pdu(bad)
            ok = False
        except ValueError:
            ok = True
        check("rejects %s pdu" % why, ok)

    print("[7] phone normalisation")
    for src, want in (("+8613800138000", "13800138000"), ("86 138 0013 8000", "13800138000"),
                      ("13800138000", "13800138000"), ("+31641600986", "31641600986")):
        check("norm %r" % src, M.normalize_phone(src) == want, M.normalize_phone(src))

    print("[8] AtModem handshake + probe (virtual EC20)")
    got_msgs = []
    vm = M.VirtualModemTransport()
    modem = M.AtModem(lambda: vm, got_msgs.append, name="virt", log=lambda *_: None)
    modem.connect()
    info = modem.probe()
    check("model", "virtual" in info.get("model", "").lower(), info.get("model"))
    check("sim ready", "READY" in info.get("sim", ""), info.get("sim"))
    check("signal", "+CSQ: 24" in info.get("signal", ""), info.get("signal"))
    check("operator", "CHINA MOBILE" in info.get("operator", ""), info.get("operator"))
    check("pdu mode", "+CMGF: 0" in info.get("pdu_mode", ""), info.get("pdu_mode"))
    check("handshake set CMGF=0", "AT+CMGF=0" in vm.commands, vm.commands[:6])
    check("handshake set CNMI", any(c.startswith("AT+CNMI=2,1") for c in vm.commands), vm.commands[:8])

    print("[9] startup sweep picks up SMS that arrived while down")
    got_msgs.clear()
    vm = M.VirtualModemTransport(messages=[("+8613800138000", "\u3010VobileMax\u3011\u9a8c\u8bc1\u7801 111111")])
    modem = M.AtModem(lambda: vm, got_msgs.append, name="virt", log=lambda *_: None)
    modem.connect()
    n = modem.sweep()
    check("one swept", n == 1, n)
    check("code text intact", got_msgs and "111111" in got_msgs[0]["text"], got_msgs)
    check("phone normalised", got_msgs and got_msgs[0]["phone"] == "13800138000", got_msgs)
    check("slot deleted", vm.deleted == [1], vm.deleted)
    check("second sweep is empty", modem.sweep() == 0)

    print("[10] duplicate delivery is emitted once")
    vm = M.VirtualModemTransport(messages=[("+8613800138000", "code 222222")])
    got_msgs.clear()
    modem = M.AtModem(lambda: vm, got_msgs.append, name="virt", delete=False,
                      log=lambda *_: None)
    modem.connect()
    modem.sweep()
    modem.read_index(1)          # same slot again, no delete
    check("one message emitted", len(got_msgs) == 1, got_msgs)
    check("duplicate counted", modem.stats["duplicates"] == 1, modem.stats)

    print("[11] live +CMTI delivery")
    vm = M.VirtualModemTransport()
    got_msgs.clear()
    modem = M.AtModem(lambda: vm, got_msgs.append, name="virt", log=lambda *_: None)
    modem.connect()
    stop = threading.Event()
    th = threading.Thread(target=modem._listen, args=(stop,), daemon=True)
    th.start()
    vm.inject("+8613900139000", "\u3010VobileMax\u3011\u60a8\u7684\u9a8c\u8bc1\u7801\u4e3a 333333")
    deadline = time.time() + 5
    while time.time() < deadline and not got_msgs:
        time.sleep(0.05)
    stop.set()
    th.join(2)
    check("live message ingested", len(got_msgs) == 1, got_msgs)
    check("live phone", got_msgs and got_msgs[0]["phone"] == "13900139000", got_msgs)
    check("live deleted", 1 in vm.deleted, vm.deleted)

    print("[12] live +CMT (direct delivery) and concatenation")
    vm = M.VirtualModemTransport()
    got_msgs.clear()
    modem = M.AtModem(lambda: vm, got_msgs.append, name="virt", log=lambda *_: None)
    modem.connect()
    stop = threading.Event()
    th = threading.Thread(target=modem._listen, args=(stop,), daemon=True)
    th.start()
    vm.inject("+8613700137000", "part-one ", ref=None) if False else None
    p1 = pdu("+8613700137000", "\u3010VobileMax\u3011\u60a8\u7684\u9a8c\u8bc1\u7801\u4e3a 444444", ref=9, total=2, seq=1)
    p2 = pdu("+8613700137000", "\uff0c5 \u5206\u949f\u5185\u6709\u6548\u3002", ref=9, total=2, seq=2)
    with vm.lock:
        vm.out.append("+CMT: ,%d" % (len(p1) // 2)); vm.out.append(p1)
        vm.out.append("+CMT: ,%d" % (len(p2) // 2)); vm.out.append(p2)
    deadline = time.time() + 5
    while time.time() < deadline and not got_msgs:
        time.sleep(0.05)
    stop.set()
    th.join(2)
    check("direct +CMT ingested", len(got_msgs) == 1, got_msgs)
    check("concat joined", got_msgs and got_msgs[0]["text"].endswith("\u6709\u6548\u3002"), got_msgs)
    check("concat code present", got_msgs and "444444" in got_msgs[0]["text"], got_msgs)

    print("[13] text mode (AT+CMGF=1)")
    vm = M.VirtualModemTransport(messages=[("+8613600136000", "\u9a8c\u8bc1\u7801 555555")])
    got_msgs.clear()
    modem = M.AtModem(lambda: vm, got_msgs.append, name="virt", pdu_mode=False,
                      log=lambda *_: None)
    modem.connect()
    check("text mode sweep", modem.sweep() == 1, modem.stats)
    check("text mode phone", got_msgs and got_msgs[0]["phone"] == "13600136000", got_msgs)
    check("text mode body", got_msgs and "555555" in got_msgs[0]["text"], got_msgs)

    print("[14] broken transport -> reconnect, no crash")
    got_msgs.clear()
    made = []

    class Flaky(M.VirtualModemTransport):
        def __init__(self, fail_after):
            super().__init__(messages=[("+8613500135000", "code 666666")] if made else [])
            self.fail_after = fail_after
            self.sent = 0

        def write(self, data):
            self.sent += 1
            if self.sent > self.fail_after:
                raise M.ModemError("cable yanked")
            super().write(data)

    def factory():
        tr = Flaky(4 if not made else 10 ** 6)
        made.append(tr)
        return tr

    modem = M.AtModem(factory, got_msgs.append, name="flaky",
                      reconnect_min=0.2, reconnect_max=0.5, log=lambda *_: None)
    stop = threading.Event()
    th = threading.Thread(target=modem.run, args=(stop,), daemon=True)
    th.start()
    deadline = time.time() + 8
    while time.time() < deadline and not got_msgs:
        time.sleep(0.05)
    stop.set()
    modem.close()
    th.join(3)
    check("reconnected at least once", modem.stats["reconnects"] >= 1, modem.stats)
    check("message after reconnect", any("666666" in m["text"] for m in got_msgs), got_msgs)
    check("modem is threadsafe enough to stop", not th.is_alive(), th.is_alive())

    print("[15] PIN-locked SIM fails loudly")
    class Locked(M.VirtualModemTransport):
        def _handle(self, cmd):
            if cmd.strip().upper().startswith("AT+CPIN?"):
                self.commands.append(cmd)
                return ["+CPIN: SIM PIN", "OK"]
            return super()._handle(cmd)

    vm = Locked()
    modem = M.AtModem(lambda: vm, lambda m: None, name="locked", log=lambda *_: None)
    try:
        modem.connect()
        msg = "connected despite PIN lock"
    except M.ModemError as exc:
        msg = str(exc)
    check("connect refuses a PIN-locked SIM", "SIM not ready" in msg and "SIM PIN" in msg, msg)
    check("transport closed after refusal", vm.closed, vm.closed)

    print("[16] vmax_smsd --modem virtual end to end")
    state = os.path.join(tempfile.mkdtemp(prefix="smsd_"), "codes.jsonl")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-u", "vmax_smsd.py", "--port", str(port),
                             "--secret", "<TEST_SECRET>", "--state", state, "--modem", "virtual"],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env)
    base = "http://127.0.0.1:%d" % port

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    try:
        deadline = time.time() + 15
        health = None
        while time.time() < deadline:
            try:
                _, health = get("/health")
                break
            except Exception:
                time.sleep(0.2)
        check("server started", health is not None, health)
        check("health lists the modem", health and health["modems"][0]["name"] == "virtual", health)
        body = urllib.parse.urlencode({"phone": "13800138000",
                                       "text": "\u3010VobileMax\u3011\u60a8\u7684\u9a8c\u8bc1\u7801\u4e3a 482913\uff0c5 \u5206\u949f\u5185\u6709\u6548\u3002"}).encode()
        req = urllib.request.Request(base + "/virtual/sms?secret=<TEST_SECRET>", data=body)
        with urllib.request.urlopen(req, timeout=10) as r:
            injected = json.loads(r.read().decode("utf-8"))
        check("inject accepted", injected.get("ok") and injected.get("index") == 1, injected)
        code = None
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                _, payload = get("/code?phone=13800138000&secret=<TEST_SECRET>&timeout=3")
                code = payload
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                time.sleep(0.2)
        check("/code returned the SMS code", code and code.get("code") == "482913", code)
        check("full body kept", code and "\u9a8c\u8bc1\u7801" in code.get("text", ""), code)
        _, health = get("/health")
        check("health counted ingest", health["ingested"] == 1 and health["stored"] == 1, health)
        check("modem stats visible", health["modems"][0]["received"] == 1, health["modems"])
        try:
            get("/code?phone=13800138000&secret=<TEST_SECRET>&since=9999999999&timeout=0.2")
            stale_status = 200
        except urllib.error.HTTPError as exc:
            stale_status = exc.code
        check("stale 'since' never returns the old code", stale_status == 404, stale_status)
        try:
            get("/code?phone=13800138000&secret=wrong&timeout=0.2")
            bad_secret = 200
        except urllib.error.HTTPError as exc:
            bad_secret = exc.code
        check("wrong secret is 401", bad_secret == 401, bad_secret)
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=5)[0].decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        print("  --- server log tail ---")
        for line in out.strip().splitlines()[-4:]:
            print("  | " + line)

    print()
    if fails:
        print("%d FAILED: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("ALL MODEM TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
