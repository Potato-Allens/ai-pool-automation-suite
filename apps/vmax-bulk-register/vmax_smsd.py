# -*- coding: utf-8 -*-
"""vmax_smsd - tiny self-hosted SMS inbox for vmax_register / vmax_batch.

Turns any real SIM into an HTTP code source:

    phone (SMS Forwarder / Tasker / 备用机脚本)
        --POST /ingest-->  vmax_smsd  --GET /code?phone=-->  vmax_register

Run:

    python vmax_smsd.py --port 8787 --secret <TEST_SECRET> --state sms_inbox/codes.jsonl

Then:

    python vmax_register.py --phone 13800138000 \
        --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=<TEST_SECRET>&timeout=180" ...

Phone-side forwarding (any of these):

    curl -X POST http://PC:8787/ingest -H "X-Sms-Secret: <TEST_SECRET>" \
         -d "phone=13800138000&text=【VobileMax】您的验证码为 482913"

    # Android: SMS Forwarder -> webhook, body {"phone":"%from%","text":"%text%"}

No phone needed at all if the card sits in a modem:

    python vmax_smsd.py --port 8787 --secret <TEST_SECRET> --modem COM3
    (see vmax_modem.py; tcp://host:port and `virtual` also work)

Endpoints
---------
POST /ingest   body JSON {phone,text} | form phone=..&text=.. ; header X-Sms-Secret
GET  /code     ?phone=..[&secret=..][&since=<unix>][&timeout=120]  long-poll
GET  /latest   ?limit=20          recent codes (no secret needed on loopback only)
GET  /health   liveness + counters

No third-party deps: stdlib only.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from vmax_console import utf8_stdout  # noqa: E402

try:
    from vmax_modem import normalize_phone
except Exception:  # pragma: no cover - vmax_modem is shipped alongside
    def normalize_phone(value: str) -> str:
        digits = re.sub(r"[^0-9]", "", value or "")
        return digits[2:] if len(digits) == 13 and digits.startswith("86") else digits

utf8_stdout()

# 验证码优先：抓「验证码/code/verification」附近的 6 位数字，避免误抓手机号、订单号
NEAR = re.compile(
    r"(?:验证码|校验码|动态码|verification\s*code|passcode|code)\D{0,12}?(\d{6})(?!\d)",
    re.IGNORECASE)
ANY6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")
PHONE_IN_TEXT = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def extract_code(text: str) -> str | None:
    """Pull the 6-digit verification code out of an SMS body."""
    if not text:
        return None
    m = NEAR.search(text)
    if m:
        return m.group(1)
    masked = PHONE_IN_TEXT.sub(" ", text)  # 手机号本身不是验证码
    m = ANY6.search(masked)
    return m.group(1) if m else None


class Store:
    def __init__(self, path: str | None, keep: int = 500):
        self.path = path
        self.keep = keep
        self.lock = threading.Lock()
        self.by_phone: dict[str, list[dict]] = {}
        self.count = 0
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f.readlines()[-self.keep:]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    self._put(rec, persist=False)
        except FileNotFoundError:
            pass

    def _put(self, rec: dict, persist: bool = True):
        phone = rec.get("phone") or ""
        self.by_phone.setdefault(phone, []).append(rec)
        if len(self.by_phone[phone]) > 50:
            self.by_phone[phone] = self.by_phone[phone][-50:]
        self.count += 1
        if persist and self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def add(self, phone: str, text: str) -> dict:
        # ts is fractional on purpose: an int-second stamp makes "arrived after the
        # send" undecidable inside one second, and then a *stale* code passes the
        # since filter (repro: probe + batch on one number inside 1 s -> /register
        # answered 验证码不正确 because the old code came back).
        rec = {"phone": phone, "text": text, "code": extract_code(text),
               "ts": round(time.time(), 3)}
        with self.lock:
            self._put(rec)
        return rec

    def get(self, phone: str, since: float | None = None,
            ttl: float | None = None) -> dict | None:
        """Newest code for *phone*, skipping stale entries.

        ``since`` drops codes that arrived before a given send-code call, so a
        resend can never pick up the previous SMS; ``ttl`` drops codes older
        than the platform's own validity window (5 min).
        """
        now = time.time()
        with self.lock:
            items = self.by_phone.get(phone) or []
            for rec in reversed(items):
                if not rec.get("code"):
                    continue
                # strict: a code that arrived at (or before) the send-code call is
                # never the answer to that call.  This also protects callers that
                # still pass an int second.
                if since and rec["ts"] <= since:
                    continue
                if ttl and now - rec["ts"] > ttl:
                    continue
                return rec
        return None

    def latest(self, limit: int = 20) -> list[dict]:
        with self.lock:
            allr = [r for v in self.by_phone.values() for r in v]
        allr.sort(key=lambda r: r["ts"], reverse=True)
        return allr[:limit]


class Handler(BaseHTTPRequestHandler):
    server_version = "vmax_smsd/1.0"
    store: Store
    secret: str = ""
    code_ttl: float = 300.0
    started = time.time()
    ingested = 0
    modems: list = []          # AtModem readers (vmax_modem)
    virtual: list = []         # VirtualModemTransport instances (demo/tests)

    # ---- helpers
    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _auth_ok(self, given: str | None) -> bool:
        if not self.secret:
            return True
        return bool(given) and hmac.compare_digest(str(given), self.secret)

    def log_message(self, fmt, *args):  # keep stderr clean by default
        if os.environ.get("SMSD_VERBOSE"):
            sys.stderr.write("[smsd] " + (fmt % args) + "\n")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype:
            try:
                return json.loads(raw) if raw.strip() else {}
            except ValueError:
                return {}
        out: dict = {}
        q = parse_qs(raw)
        for k, v in q.items():
            out[k] = v[0]
        return out

    def _virtual_sms(self, u):
        """Demo/test hook: push an SMS into the virtual modem's AT stack."""
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        data = self._body()
        secret = (data.get("secret") or self.headers.get("X-Sms-Secret") or q.get("secret"))
        if not self._auth_ok(str(secret) if secret else None):
            return self._json(401, {"ok": False, "error": "bad secret"})
        if not Handler.virtual:
            return self._json(409, {"ok": False,
                                    "error": "no virtual modem attached (start with --modem virtual)"})
        phone = str(data.get("phone") or q.get("phone") or "")
        text = str(data.get("text") or q.get("text") or "")
        if not text.strip():
            return self._json(400, {"ok": False, "error": "empty text"})
        idx = Handler.virtual[0].inject("+" + normalize_phone(phone) if phone else "", text)
        return self._json(200, {"ok": True, "index": idx})

    # ---- routes
    def do_POST(self):
        u = urlparse(self.path)
        if u.path.rstrip("/") == "/virtual/sms":
            return self._virtual_sms(u)
        if u.path.rstrip("/") not in ("/ingest", "/sms", "/webhook"):
            return self._json(404, {"ok": False, "error": "not found"})
        data = self._body()
        q = parse_qs(u.query)
        phone = str(data.get("phone") or (q.get("phone") or [""])[0]).strip()
        text = str(data.get("text") or data.get("message") or data.get("body")
                   or (q.get("text") or [""])[0])
        secret = (data.get("secret") or self.headers.get("X-Sms-Secret")
                  or self.headers.get("X-Secret") or (q.get("secret") or [""])[0])
        if not self._auth_ok(str(secret) if secret else None):
            return self._json(401, {"ok": False, "error": "bad secret"})
        phone = re.sub(r"[^\d]", "", phone)
        if len(phone) == 13 and phone.startswith("86"):
            phone = phone[2:]
        if not re.fullmatch(r"1[3-9]\d{9}", phone):
            return self._json(400, {"ok": False, "error": "phone must be 11-digit mainland number"})
        if not text.strip():
            return self._json(400, {"ok": False, "error": "empty text"})
        rec = self.store.add(phone, text)
        Handler.ingested += 1
        return self._json(200, {"ok": True, "code": rec["code"], "ts": rec["ts"]})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/") or "/"
        if path == "/health":
            return self._json(200, {"ok": True, "uptime": int(time.time() - Handler.started),
                                    "ingested": Handler.ingested, "stored": self.store.count,
                                    "modems": [dict(name=m.name, **m.stats)
                                               for m in Handler.modems]})
        if path == "/latest":
            # codes are secrets: never expose them just because the port is open
            if not self._auth_ok(q.get("secret") or self.headers.get("X-Sms-Secret")):
                return self._json(401, {"ok": False, "error": "bad secret"})
            return self._json(200, {"ok": True, "items": self.store.latest(int(q.get("limit", 20) or 20))})
        if path == "/code":
            if not self._auth_ok(q.get("secret") or self.headers.get("X-Sms-Secret")):
                return self._json(401, {"ok": False, "error": "bad secret"})
            phone = re.sub(r"[^\d]", "", q.get("phone", ""))
            if len(phone) == 13 and phone.startswith("86"):
                phone = phone[2:]
            if not phone:
                return self._json(400, {"ok": False, "error": "phone required"})
            since = None
            if q.get("since"):
                try:
                    since = float(q["since"])
                except ValueError:
                    since = None
            try:
                ttl = float(q.get("ttl", self.code_ttl) or 0)
            except ValueError:
                ttl = self.code_ttl
            ttl = min(max(ttl, 0.0), 3600.0)
            wait = min(float(q.get("timeout", 0) or 0), 300.0)
            deadline = time.time() + max(wait, 0)
            while True:
                rec = self.store.get(phone, since, ttl or None)
                if rec:
                    return self._json(200, {"ok": True, "code": rec["code"], "text": rec["text"],
                                            "ts": rec["ts"]})
                if time.time() >= deadline:
                    return self._json(404, {"ok": False, "code": None, "error": "timeout",
                                            "phone": phone})
                time.sleep(0.5)
        return self._json(404, {"ok": False, "error": "not found"})


SELF_TESTS = [
    ("【VobileMax】您的验证码为 482913，5 分钟内有效。", "482913"),
    ("验证码：193827", "193827"),
    ("G-704512 is your VobileMax verification code", "704512"),
    ("Your code: 660021. Do not share.", "660021"),
    ("您的动态码 338291 已发送，手机 13800138000", "338291"),
    ("order 20241026123 shipped", None),
]


def self_test() -> int:
    bad = 0
    for text, want in SELF_TESTS:
        got = extract_code(text)
        flag = "ok " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"[{flag}] {text[:48]!r} -> {got!r} (want {want!r})")
    print("self-test:", "PASS" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="self-hosted SMS inbox for vmax_register")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--secret", default=os.environ.get("SMSD_SECRET", ""))
    ap.add_argument("--state", default="sms_inbox/codes.jsonl")
    ap.add_argument("--code-ttl", type=float, default=300.0,
                    help="ignore codes older than this many seconds (platform codes live ~5 min)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--modem", action="append", metavar="SPEC",
                    help="real SIM reader: COM3 / serial://COM3 / tcp://host:port / virtual "
                         "(repeatable for a card pool)")
    ap.add_argument("--modem-baud", type=int, default=115200)
    ap.add_argument("--modem-mode", choices=("pdu", "text"), default="pdu",
                    help="PDU is what Chinese operators send; text mode is a fallback")
    ap.add_argument("--modem-keep", action="store_true",
                    help="do not delete SIM slots after ingest (default: delete)")
    ap.add_argument("--modem-probe", action="store_true",
                    help="print model/SIM/signal/number for each --modem and exit")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    Handler.store = Store(ns.state)
    Handler.secret = ns.secret
    Handler.code_ttl = ns.code_ttl

    readers = []
    if ns.modem:
        try:
            import vmax_modem as modem_mod
        except Exception as exc:
            print("[smsd] --modem needs vmax_modem.py next to this file: %s" % exc, flush=True)
            return 2
        if ns.modem_probe:
            out = {}
            bad = 0
            for spec in ns.modem:
                m = modem_mod.AtModem(lambda s=spec: modem_mod.parse_modem_spec(s, ns.modem_baud),
                                      lambda msg: None, name=spec, delete=False,
                                      pdu_mode=(ns.modem_mode == "pdu"))
                try:
                    out[spec] = m.probe()
                except Exception as exc:
                    out[spec] = {"error": str(exc)}
                    bad += 1
                finally:
                    m.close()
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 1 if bad else 0

        def _on_message(msg):
            rec = Handler.store.add(msg["phone"], msg["text"])
            Handler.ingested += 1
            print("[smsd] modem %s -> %s code=%s %r"
                  % (msg.get("source"), msg["phone"], rec.get("code"),
                     msg["text"][:40]), flush=True)

        for spec in ns.modem:
            m = modem_mod.AtModem(lambda s=spec: modem_mod.parse_modem_spec(s, ns.modem_baud),
                                  _on_message, name=spec,
                                  delete=not ns.modem_keep,
                                  pdu_mode=(ns.modem_mode == "pdu"),
                                  log=lambda line: print(line, flush=True))
            readers.append(m)
            Handler.modems.append(m)
            if spec.strip().lower() in ("virtual", "demo", "loopback"):
                tr = modem_mod.VirtualModemTransport()
                Handler.virtual.append(tr)
                m.factory = lambda tr=tr: tr
    loopback = ns.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not ns.secret and not os.environ.get("SMSD_ALLOW_OPEN"):
        print("[smsd] refusing to bind " + str(ns.host) + " without --secret: the inbox would be "
              "readable by anyone on the LAN (set SMSD_ALLOW_OPEN=1 to override)", flush=True)
        return 2
    srv = ThreadingHTTPServer((ns.host, ns.port), Handler)
    print(f"[smsd] listening on http://{ns.host}:{ns.port}  secret={'set' if ns.secret else 'OFF'}  "
          f"state={ns.state}", flush=True)
    if not ns.secret:
        print("[smsd] note: no --secret, loopback-only usage assumed", flush=True)
    for m in readers:
        m.start()
        print("[smsd] modem reader started: %s" % m.name, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        for m in readers:
            m.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())