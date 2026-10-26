# -*- coding: utf-8 -*-
"""Offline checks for the one-card protocol experiments (no real card, no SMS).

The mock auth server can bind the code to the sending session or not, which is
exactly the question `vmax_onecard.py xdevice` answers in production.  Both
verdicts, an expired code, a 429, a dead channel and the TTL bisect are driven
through the real experiment code.

R133 adds `nosms`: a code is pending, but `/register` carries no `sms_code` (then
`sms_code=""`).  The mock can play four handlers -- strict, "omit is fine",
"empty is fine", and "no pending record" -- so all four verdicts are pinned.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["VMAX_NOPACE"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vmax_client as C  # noqa: E402
import vmax_onecard as O  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


class Cfg:
    base = ""
    sms_dir = ""
    bound = False          # tie the code to the cookie that asked for it
    ttl = 300.0            # seconds the code stays valid
    rate_limit = False     # 429 on send-code
    code_policy = "strict"  # strict | omit_ok | empty_ok
    drop_codes = False     # pretend /register cannot see the pending record
    sends = 0
    writes = 0
    counter = 0
    codes = {}             # phone -> (code, ts, sid)
    forms = []             # every /register body, in order (R133)
    tokens = 0
    logins = 0             # GET /login (sets auth_device)
    checks = 0             # GET /api/auth/signup-check
    device_refuse = False  # signup-check says "you still need a captcha"
    device_ignore = False  # signup-check says false but /register disagrees
    login_status = 200     # a bare /login answers 422 on production (R142)
    login_sets_cookie = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text, extra=None):
        body = ("<html><body><p>%s</p></body></html>" % text).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode("utf-8", "replace") if n else ""

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k:
                out[k] = v
        return out

    def _sid(self):
        return self._cookies().get("sid", "")

    def _device_ok(self):
        """What /register believes.  signup-check is asked separately, so
        ``device_ignore`` can model "check said false, the write path disagreed"."""
        if Cfg.device_ignore:
            return False
        c = self._cookies()
        return bool(c.get("auth_device")) and bool(c.get("auth_fingerprint"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/auth/agreements":
            return self._json(200, [{"id": "1"}, {"id": "2"}])
        if u.path == "/login":
            Cfg.logins += 1
            body = (b"<html><body><form id=\"register-form\" "
                    b"action=\"/register\"></form></body></html>")
            self.send_response(Cfg.login_status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if Cfg.login_sets_cookie:
                self.send_header("Set-Cookie",
                                 "auth_device=DEV-%d; Path=/" % Cfg.logins)
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/auth/signup-check":
            Cfg.checks += 1
            c = self._cookies()
            trusted = (bool(c.get("auth_device")) and bool(c.get("auth_fingerprint"))
                       and not Cfg.device_refuse)
            return self._json(200, {"requires_captcha": not trusted})
        return self._json(404, {"detail": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/register/send-code":
            return self.send_code()
        if u.path == "/register":
            return self.register()
        if u.path == "/api/auth/oauth/token":
            return self.token()
        return self._json(404, {"detail": "not found"})

    def send_code(self):
        Cfg.sends += 1
        if Cfg.rate_limit:
            return self._json(429, {"ok": False, "message": "操作过于频繁"})
        body = json.loads(self._body() or "{}")
        phone = str(body.get("phone") or "")
        if not body.get("captcha_verify_param"):
            return self._json(400, {"ok": False, "message": "Captcha verification rejected"})
        sid = self._sid()
        extra = {}
        if not sid:
            sid = "sid-%d" % (Cfg.sends,)
            extra["Set-Cookie"] = "sid=%s; Path=/" % sid
        Cfg.counter += 1
        code = "%06d" % Cfg.counter
        Cfg.codes[phone] = (code, time.time(), sid)
        with open(os.path.join(Cfg.sms_dir, phone + ".txt"), "w", encoding="utf-8") as f:
            f.write("【VobileMax】您的验证码为 %s，5 分钟内有效。" % code)
        return self._json(200, {"ok": True, "message": "验证码已发送", "code": None}, extra)

    def register(self):
        Cfg.writes += 1
        form = {k: v[0] for k, v in
                urllib.parse.parse_qs(self._body(), keep_blank_values=True).items()}
        Cfg.forms.append(dict(form))
        if not form.get("captcha_verify_param") and not self._device_ok():
            return self._html(400, "Unsupported captcha platform")
        phone = form.get("phone", "")
        rec = None if Cfg.drop_codes else Cfg.codes.get(phone)
        if not rec:
            return self._html(400, "请先获取短信验证码")
        code, ts, sid = rec
        if time.time() - ts > Cfg.ttl:
            return self._html(400, "验证码已过期，请重新获取")
        if Cfg.bound and self._sid() != sid:
            return self._html(400, "验证码不正确")
        if Cfg.code_policy == "omit_ok" and "sms_code" not in form:
            pass                                    # the pending record is enough
        elif Cfg.code_policy == "empty_ok" and form.get("sms_code") == "":
            pass                                    # "" is accepted like a real code
        elif form.get("sms_code") != code:
            return self._html(400, "验证码不正确")
        state = form.get("state", "")
        loc = "%s/auth/callback?code=CODE-%s&state=%s" % (Cfg.base, code, state)
        return self._html(303, "redirect", {"Location": loc})

    def token(self):
        Cfg.tokens += 1
        return self._json(200, {"access_token": "AT-MOCK", "token_type": "Bearer",
                                "expires_in": 3600,
                                "user": {"uid": "uid-%d" % Cfg.tokens}})


class Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        Cfg.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def reset(**kw):
    Cfg.bound = kw.get("bound", False)
    Cfg.ttl = kw.get("ttl", 300.0)
    Cfg.rate_limit = kw.get("rate_limit", False)
    Cfg.code_policy = kw.get("code_policy", "strict")
    Cfg.drop_codes = kw.get("drop_codes", False)
    Cfg.login_status = kw.get("login_status", 200)
    Cfg.login_sets_cookie = kw.get("login_sets_cookie", True)
    Cfg.device_refuse = kw.get("device_refuse", False)
    Cfg.device_ignore = kw.get("device_ignore", False)
    Cfg.sends = Cfg.writes = Cfg.counter = Cfg.tokens = 0
    Cfg.logins = Cfg.checks = 0
    Cfg.codes = {}
    Cfg.forms = []


def make_args(mode, tmp, **kw):
    Cfg.sms_dir = tmp          # where the mock drops <phone>.txt
    args = [mode, "--phone", kw.pop("phone", "13800138000"),
            "--sms", "file:" + tmp, "--sms-dir", tmp,
            "--sms-timeout", "5", "--captcha", "param", "--captcha-param", "TOK",
            "--base", Cfg.base, "--redirect", Cfg.base + "/auth/callback",
            "--out", os.path.join(tmp, "onecard.json")]
    for k, v in kw.items():
        name = "--" + k.replace("_", "-")
        if v is True:                  # store_true style flags
            args.append(name)
        elif v is not False:
            args += [name, str(v)]
    return args


def verdict(tmp):
    with open(os.path.join(tmp, "onecard.json"), encoding="utf-8") as f:
        return json.load(f)["verdict"]


def results(tmp):
    p = os.path.join(tmp, "onecard.results.jsonl")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

def main():
    print("[0] dry run: plan printed, nothing spent, no verdict file")
    with tempfile.TemporaryDirectory() as td:
        rc = O.main(["xdevice", "--phone", "13800138000", "--dry-run",
                     "--out", os.path.join(td, "onecard.json")])
        check("exit 0", rc == 0, rc)
        check("no file written", not os.path.exists(os.path.join(td, "onecard.json")))
        rc = O.main(["ttl", "--phone", "+8613800138000", "--dry-run",
                     "--out", os.path.join(td, "t.json")])
        check("+86 is normalised, not rejected", rc == 0, rc)
        rc = O.main(["xdevice", "--phone", "123", "--dry-run"])
        check("a bad phone is exit 2", rc == 2, rc)

    print("[1] xdevice: the code follows the phone, not the session")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        rc = O.main(make_args("xdevice", td, rounds=1))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("verdict is cross_session_accepted", v["answer"] == O.CODE_ACCEPTED, v)
        check("one SMS was requested", Cfg.sends == 1, Cfg.sends)
        check("one /register write", Cfg.writes == 1, Cfg.writes)
        check("the 303 code was exchanged", Cfg.tokens == 1, Cfg.tokens)
        check("the account was recorded", len(results(td)) == 1, results(td))
        row = results(td)[0]
        check("row is usable by vmax_login",
              row["status"] == "ok" and row["uid"] == "uid-1"
              and row["username"] and row["password"], row)
        check("row says where it came from", row["via"] == "onecard_xdevice", row)
        raw = open(os.path.join(td, "onecard.json"), encoding="utf-8").read()
        doc = json.loads(raw)
        check("verdict names the sessions", doc["verdict"]["sent_in"] == "A"
              and doc["verdict"]["submitted_in"] == "B", doc["verdict"])
        check("the verdict keeps the token summary",
              doc["verdict"]["has_access_token"] is True, doc["verdict"])
        check("no password in the verdict", "password" not in raw)
        check("no access token in the verdict", "AT-MOCK" not in raw)

    print("[2] xdevice: a session-bound code is refused, and that is a verdict")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(bound=True)
        rc = O.main(make_args("xdevice", td))
        v = verdict(td)
        check("exit 0 (an answer, not an error)", rc == 0, rc)
        check("verdict is cross_session_rejected", v["answer"] == O.CODE_BOUND, v)
        check("the server said 验证码不正确", "验证码不正确" in (v["register"]["error"] or ""),
              v["register"])
        check("no account was created", results(td) == [], results(td))
        check("exactly one write", Cfg.writes == 1, Cfg.writes)

    print("[3] xdevice: an expired code is inconclusive, not an answer")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(ttl=0.0)
        rc = O.main(make_args("xdevice", td))
        v = verdict(td)
        check("exit 1", rc == 1, rc)
        check("verdict is inconclusive_expired", v["answer"] == "inconclusive_expired", v)
        check("no account", results(td) == [], results(td))

    print("[4] a 429 on send-code stops before anything else")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(rate_limit=True)
        rc = O.main(make_args("xdevice", td))
        v = verdict(td)
        check("exit 3", rc == 3, rc)
        check("verdict is rate_limited", v["answer"] == "rate_limited", v)
        check("no register write", Cfg.writes == 0, Cfg.writes)

    print("[5] a dead SMS channel costs a token no more")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        missing = os.path.join(td, "no_such_dir")
        rc = O.main(["xdevice", "--phone", "13800138000", "--sms", "file:" + missing,
                     "--sms-dir", missing, "--captcha", "param", "--captcha-param", "TOK",
                     "--base", Cfg.base, "--redirect", Cfg.base + "/auth/callback",
                     "--out", os.path.join(td, "onecard.json")])
        check("exit 9", rc == 9, rc)
        check("0 sends", Cfg.sends == 0, Cfg.sends)
        check("no verdict file", not os.path.exists(os.path.join(td, "onecard.json")))
    print("[6] ttl: an expiring code bisects downwards")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(ttl=0.01)
        rc = O.main(make_args("ttl", td, low=0, high=0.05, rounds=2, precision=0.01,
                              cooldown=0))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("answer is ttl_between", v["answer"] == "ttl_between", v)
        check("the upper bound moved below the first probe",
              v["ttl_max_s"] <= 0.025, v)
        check("two probes were paid for", Cfg.sends == 2, Cfg.sends)
        check("the round log has both probes", len(v["rounds"]) == 2, v["rounds"])
        check("no account was created", results(td) == [], results(td))

    print("[7] ttl: a live code is a lower bound, and the account is kept")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(ttl=300.0)
        rc = O.main(make_args("ttl", td, low=0, high=1, rounds=3, precision=0.1,
                              cooldown=0))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("answer is ttl_between", v["answer"] == "ttl_between", v)
        check("ttl_min is the probe that worked", v["ttl_min_s"] == 0.5, v)
        check("the run stops after the account exists", Cfg.sends == 1, Cfg.sends)
        check("the account is on disk", len(results(td)) == 1, results(td))
        check("and it came from the ttl mode", results(td)[0]["via"] == "onecard_ttl",
              results(td))

    print("[8] the verdict file is always machine readable")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        rc = O.main(make_args("xdevice", td))
        doc = json.loads(open(os.path.join(td, "onecard.json"), encoding="utf-8").read())
        check("exit 0", rc == 0, rc)
        check("tool name", doc["tool"] == "vmax_onecard", doc.get("tool"))
        check("mode", doc["mode"] == "xdevice", doc.get("mode"))
        check("verdict block", isinstance(doc["verdict"], dict)
              and doc["verdict"]["answer"] == O.CODE_ACCEPTED, doc.get("verdict"))
        check("elapsed is recorded", isinstance(doc["verdict"].get("elapsed_s"), float),
              doc["verdict"].get("elapsed_s"))

    print("[9] the contract gate runs before the first send-code")
    real_gate = O.contract_gate
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        seen = []

        def drift(a):
            seen.append(a.contract)
            return 1, "[contract] DRIFT: path /register is gone"

        O.contract_gate = drift
        try:
            rc = O.main(make_args("xdevice", td, contract=os.path.join(td, "base.json")))
        finally:
            O.contract_gate = real_gate
        v = verdict(td)
        doc = json.loads(open(os.path.join(td, "onecard.json"), encoding="utf-8").read())
        check("exit 4 on drift (its own code, not 1/2)", rc == 4, rc)
        check("verdict is contract_drift", v["answer"] == O.CONTRACT_DRIFT, v)
        check("0 SMS spent", Cfg.sends == 0, Cfg.sends)
        check("0 register writes", Cfg.writes == 0, Cfg.writes)
        check("0 accounts", results(td) == [], results(td))
        check("the baseline was handed to the gate",
              seen == [os.path.join(td, "base.json")], seen)
        check("the verdict records the contract exit",
              doc.get("contract", {}).get("exit") == 1, doc.get("contract"))

    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        O.contract_gate = lambda a: (2, "[contract] unavailable: /health/live is not 200")
        try:
            rc = O.main(make_args("xdevice", td, contract=os.path.join(td, "base.json")))
        finally:
            O.contract_gate = real_gate
        v = verdict(td)
        check("exit 4 when the contract cannot be checked", rc == 4, rc)
        check("verdict is contract_unavailable", v["answer"] == O.CONTRACT_UNAVAILABLE, v)
        check("still 0 SMS", Cfg.sends == 0, Cfg.sends)

    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        O.contract_gate = lambda a: (1, "[contract] DRIFT")
        try:
            rc = O.main(make_args("xdevice", td, contract=os.path.join(td, "base.json"),
                                  force=True))
        finally:
            O.contract_gate = real_gate
        v = verdict(td)
        check("--force still runs the experiment",
              rc == 0 and v["answer"] == O.CODE_ACCEPTED, (rc, v.get("answer")))
        check("--force spent the one SMS", Cfg.sends == 1, Cfg.sends)

    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        seen = []

        def ok(a):
            seen.append(a.contract)
            return 0, "[contract] no drift"

        O.contract_gate = ok
        try:
            rc = O.main(make_args("xdevice", td))
        finally:
            O.contract_gate = real_gate
        check("without --contract the gate is a no-op",
              seen == [None] and rc == 0, (seen, rc))

    print("[10] nosms (strict): absent and empty are both just a wrong code")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        rc = O.main(make_args("nosms", td))
        v = verdict(td)
        check("exit 0 (a verdict, not an error)", rc == 0, rc)
        check("answer is code_value_required", v["answer"] == "code_value_required", v)
        check("both variants are in the record",
              [a["variant"] for a in v["attempts"]] == ["omit", "empty"], v["attempts"])
        check("the omitted request really had no sms_code key",
              "sms_code" not in Cfg.forms[0], Cfg.forms[0])
        check("the second request sent an empty string",
              Cfg.forms[1].get("sms_code") == "", Cfg.forms[1])
        check("one SMS only", Cfg.sends == 1, Cfg.sends)
        check("two /register writes", Cfg.writes == 2, Cfg.writes)
        check("no account", results(td) == [], results(td))

    print("[11] nosms (omit_ok): the pending record alone creates the account")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(code_policy="omit_ok")
        rc = O.main(make_args("nosms", td))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("answer is accepted_without_code",
              v["answer"] == "accepted_without_code", v)
        check("one SMS, one write", (Cfg.sends, Cfg.writes) == (1, 1),
              (Cfg.sends, Cfg.writes))
        check("the 303 code was exchanged", Cfg.tokens == 1, Cfg.tokens)
        row = results(td)
        check("the account was recorded", len(row) == 1, row)
        check("row says nosms", row and row[0]["via"] == "onecard_nosms", row)
        check("no second attempt was needed", len(v["attempts"]) == 1, v["attempts"])
        check("the verdict keeps the token summary",
              v.get("account", {}).get("has_access_token") is True, v.get("account"))
        raw = open(os.path.join(td, "onecard.json"), encoding="utf-8").read()
        check("no password / token in the verdict",
              "password" not in raw and "AT-MOCK" not in raw)

    print("[12] nosms (empty_ok): only the empty-string variant is accepted")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(code_policy="empty_ok")
        rc = O.main(make_args("nosms", td))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("answer is accepted_with_empty_code",
              v["answer"] == "accepted_with_empty_code", v)
        check("both variants ran",
              [a["answer"] for a in v["attempts"]]
              == ["code_value_required", "accepted_with_empty_code"], v["attempts"])
        check("still one SMS", Cfg.sends == 1, Cfg.sends)
        check("the account exists", len(results(td)) == 1, results(td))

    print("[13] nosms: a pending record the server cannot see is its own verdict")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(drop_codes=True)
        rc = O.main(make_args("nosms", td))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("answer is no_pending_code_seen",
              v["answer"] == "no_pending_code_seen", v)
        check("it stops after the first attempt", len(v["attempts"]) == 1, v["attempts"])
        check("one write, no account", (Cfg.writes, results(td)) == (1, []),
              (Cfg.writes, results(td)))

    print("[14] nosms: dry run and arg validation")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "n.json")
        rc = O.main(["nosms", "--phone", "13800138000", "--dry-run", "--out", out])
        check("exit 0", rc == 0, rc)
        check("no file written", not os.path.exists(out))
        rc = O.main(["nosms", "--phone", "123", "--dry-run"])
        check("a bad phone is exit 2", rc == 2, rc)
        check("nosms is an accepted mode", "nosms" in O.build_parser()._actions[1].choices,
              O.build_parser()._actions[1].choices)

    print("[15] device: the /register form carries no token at all")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        rc = O.main(make_args("xdevice", td))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("registered", v["answer"] == O.CODE_ACCEPTED, v)
        check("the form sent an empty captcha param",
              Cfg.forms[-1].get("captcha_verify_param", "") == "", Cfg.forms[-1])
        check("signup-check was asked exactly once", Cfg.checks == 1, Cfg.checks)
        check("GET /login ran first (it is what sets auth_device)",
              Cfg.logins >= 1, Cfg.logins)
        check("the verdict says device", v["form"]["form_captcha"] == "device",
              v["form"])
        check("and signup-check said no captcha",
              (v["form"].get("device") or {}).get("ok") is True, v["form"])
        check("one SMS, one write -- the SMS is still paid",
              (Cfg.sends, Cfg.writes) == (1, 1), (Cfg.sends, Cfg.writes))
        check("the account still came out", len(results(td)) == 1, results(td))

    print("[16] device refused by signup-check -> one paid token for the form")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(device_refuse=True)
        rc = O.main(make_args("xdevice", td))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("still registers", v["answer"] == O.CODE_ACCEPTED, v)
        check("the form sent the paid token",
              Cfg.forms[-1].get("captcha_verify_param") == "TOK", Cfg.forms[-1])
        check("policy recorded as device_fallback",
              v["form"]["form_captcha"] == "device_fallback", v["form"])
        check("device.ok is False", (v["form"].get("device") or {}).get("ok") is False,
              v["form"])

    print("[17] device trusted but /register disagrees -> one token, one resubmit")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(device_ignore=True)
        rc = O.main(make_args("xdevice", td))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("registers after the retry", v["answer"] == O.CODE_ACCEPTED, v)
        check("the form was called twice", Cfg.writes == 2, Cfg.writes)
        check("first empty, then a real token",
              [f.get("captcha_verify_param", "") for f in Cfg.forms] == ["", "TOK"],
              Cfg.forms)
        check("policy recorded as device_retry",
              v["form"]["form_captcha"] == "device_retry", v["form"])
        check("the retry flag is set", v["form"].get("captcha_retry") is True, v["form"])
        check("production wording is what triggered it",
              O.classify_register_error("Unsupported captcha platform") == "captcha_rejected")

    print("[18] --form-captcha auto keeps the old shape: a token on the form")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        rc = O.main(make_args("xdevice", td, form_captcha="auto"))
        v = verdict(td)
        check("exit 0", rc == 0, rc)
        check("registered", v["answer"] == O.CODE_ACCEPTED, v)
        check("the form sent the token",
              Cfg.forms[-1].get("captcha_verify_param") == "TOK", Cfg.forms[-1])
        check("no signup-check was asked", Cfg.checks == 0, Cfg.checks)
        check("policy recorded as auto", v["form"]["form_captcha"] == "auto", v["form"])

    print("[19a] the fingerprint rule matches the server: 32 lowercase hex (R144)")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset()
        before = Cfg.logins
        for bad in ("ABC", "f" * 31, "f" * 33, "a b c", "1234567890"):
            auth = C.VmaxAuth(base=Cfg.base)
            raised = None
            try:
                auth.prepare_device(fingerprint=bad, login_url=Cfg.base + "/login")
            except C.VmaxError as e:
                raised = e
            check("refused locally: %r" % (bad if len(bad) < 12 else bad[:8] + "..."),
                  raised is not None, raised)
        check("no request was made for a bad fingerprint", Cfg.logins == before, Cfg.logins)

    print("[19] a 422 /login that still hands out auth_device is good enough (R142)")
    with Server() as srv, tempfile.TemporaryDirectory() as td:
        reset(login_status=422)
        auth = C.VmaxAuth(base=Cfg.base)
        info = auth.prepare_device(login_url=Cfg.base + "/login")
        check("prepare_device does not raise", info.get("requires_captcha") is False, info)
        check("the cookie really came from the 422",
              "auth_device" in auth.s.cookies.get_dict(), auth.s.cookies.get_dict())
        check("the fingerprint is in the jar too",
              "auth_fingerprint" in auth.s.cookies.get_dict(), auth.s.cookies.get_dict())
        reset(login_status=422, login_sets_cookie=False)
        auth = C.VmaxAuth(base=Cfg.base)
        raised = None
        try:
            auth.prepare_device(login_url=Cfg.base + "/login")
        except C.VmaxError as e:
            raised = e
        check("a 4xx without the cookie is still fatal", raised is not None, raised)

    print("ALL ONECARD TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())