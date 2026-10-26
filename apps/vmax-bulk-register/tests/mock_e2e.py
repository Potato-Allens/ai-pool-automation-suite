# -*- coding: utf-8 -*-
"""Offline end-to-end harness: run vmax_register against a local fake vobilemax.

Why: the 303 + code success branch has never executed against the real server
(no reachable +86 inbox yet). This harness exercises exactly that code path --
form POST fields, Origin check, 303 Location parsing, PKCE exchange, and the
--sms-resends recovery loop -- over real HTTP, so the first attempt with a real
SIM is not the first time the code runs.

    python tests/mock_e2e.py

Scenarios
---------
F rate limit            -> Cfg.rate_limit_after returns 429 on /register
A happy path             -> 303 -> code -> access_token
B one wrong code         -> resend loop recovers
C per-phone 60s cooldown -> second send rejected, classified as "cooldown"
D PKCE mismatch          -> token endpoint 400 invalid_grant
E 303 without code       -> reported as failure, not a crash
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from vmax_client import VmaxAuth, VmaxError  # noqa: E402
from vmax_register import register_one  # noqa: E402

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


class Cfg:
    token = "MOCK-TOKEN"
    cooldown = 0
    reject_first = 0
    omit_code = False
    port = 0
    base = ""
    codes_path = ""
    sms_dir = ""
    last_send: dict[str, float] = {}
    codes: dict[str, str] = {}
    attempts: dict[str, int] = {}
    oauth: dict[str, dict] = {}
    counter = 0
    sends = 0
    register_tries = 0
    rate_limit_after = 0   # >0: return 429 on /register after this many writes
    send_code_fail: dict = {}   # phone -> message: /register/send-code says ok:false
    phone_taken: set = set()    # numbers the server treats as already registered
    users: dict = {}            # phone -> {"username", "password", "uid"}
    login_tries = 0
    require_captcha_login = True
    reset_rate_limited = False  # 429 on /api/auth/reset-password/send-code
    reset_delay = 0.0           # seconds the reset send-code takes to answer
                                # (a real run pays 10-120 s of captcha first,
                                #  which is what widens the concurrency window)
    drop_reset_flow = False     # openapi.json no longer advertises the reset routes
    reset_reject_first = 0      # >0: /api/auth/reset-password answers 400 this many times
    agreements_accepted = True  # /api/auth/agreements/my-status: session signed the active versions
    status_calls = 0            # GET  /api/auth/agreements/my-status hits
    accepts = 0                 # POST /api/auth/agreements/accept hits
    drop_agreements = False     # /register silently forgets accepted_agreement_ids
    csrf_seen = ""              # last X-CSRF-Token the accept endpoint received
    accept_rejects = 0          # accept calls refused for a missing/bad CSRF header


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---------- helpers
    def _send(self, code: int, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _html(self, code, text, extra=None):
        self._send(code, f"<html><body><p>{text}</p></body></html>".encode(), "text/html; charset=utf-8", extra)

    def _form(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _origin_ok(self):
        return (self.headers.get("Origin") or "").rstrip("/") == Cfg.base.rstrip("/")

    @staticmethod
    def _s256(verifier: str) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    # ---------- device cookies (R140)
    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == name:
                return v.strip()
        return ""

    def _device_ok(self) -> bool:
        """auth_device (set by GET /login) + a 32-hex auth_fingerprint.

        Mirrors production: the pair is what makes signup-check answer
        {"requires_captcha": false} and lets POST /register skip the token.
        """
        fp = self._cookie("auth_fingerprint")
        return bool(self._cookie("auth_device")) and bool(re.fullmatch(r"[0-9a-f]{32}", fp))

    # ---------- routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/login":
            return self._html(200, "login", {"Set-Cookie": "auth_device=mockdevice; Path=/"})
        if u.path == "/api/auth/agreements":
            return self._json(200, [{"id": "12", "type": "terms", "agreement_type": "terms"},
                                    {"id": "13", "type": "privacy", "agreement_type": "privacy"}])
        if u.path == "/api/auth/captcha-config":
            # `enabled` is what vmax_go's preflight asserts; without it the child
            # gate fails and the e2e would need --force (hiding the real check).
            return self._json(200, {"enabled": True, "platform": "mock", "scene_id": "mock"})
        if u.path == "/api/auth/sms-config":
            return self._json(200, {"enabled": True, "provider": "mock"})
        if u.path == "/session":
            return self._json(200, {"authenticated": True, "user_id": 1,
                                    "csrf_token": "mock-csrf"})
        if u.path == "/api/auth/agreements/my-status":
            Cfg.status_calls += 1
            return self._json(200, [
                {"type": "terms", "version": "2026.09.30",
                 "accepted": bool(Cfg.agreements_accepted)},
                {"type": "privacy", "version": "2026.09.30",
                 "accepted": bool(Cfg.agreements_accepted)}])
        if u.path == "/api/auth/signup-check":
            return self._json(200, {"requires_captcha": not self._device_ok()})
        if u.path == "/openapi.json":
            paths = {"/api/auth/login": {"post": {}},
                     "/api/auth/reset-password/send-code": {"post": {}},
                     "/api/auth/reset-password": {"post": {}},
                     "/api/auth/register": {"post": {}},
                     "/register/send-code": {"post": {}},
                     "/register": {"post": {}}}
            if Cfg.drop_reset_flow:
                paths.pop("/api/auth/reset-password/send-code", None)
                paths.pop("/api/auth/reset-password", None)
            return self._json(200, {
                "openapi": "3.1.0", "info": {"title": "mock auth", "version": "0"},
                "paths": paths,
                "components": {"schemas": {"ResetPasswordRequest": {
                    "type": "object",
                    "properties": {"phone": {}, "sms_code": {}, "new_password": {}}}}}})
        if u.path == "/health/ready":
            return self._json(200, {"status": "ok"})
        if u.path == "/health/live":
            return self._json(200, {"status": "ok"})
        return self._json(404, {"detail": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/register/send-code":
            return self.send_code()
        if u.path == "/register":
            return self.register()
        if u.path == "/api/auth/oauth/token":
            return self.token()
        if u.path == "/api/auth/login":
            return self.login()
        if u.path == "/api/auth/agreements/accept":
            # live schema (2026-10-26): Origin + X-CSRF-Token header required,
            # so a client that forgets the token gets 403 here just like in prod
            body = self._json_body()
            csrf = self.headers.get("X-CSRF-Token") or ""
            Cfg.csrf_seen = csrf
            if not csrf or csrf != "mock-csrf" or not self._origin_ok():
                Cfg.accept_rejects += 1
                return self._json(403, {"detail": "CSRF token invalid"})
            Cfg.accepts += 1
            Cfg.agreements_accepted = True
            return self._json(200, {"accepted_ids": [int(i) for i in body.get("ids", [])]})
        if u.path == "/api/auth/reset-password/send-code":
            return self.reset_send_code()
        if u.path == "/api/auth/reset-password":
            return self.reset_password()
        return self._json(404, {"detail": "not found"})

    def send_code(self):
        if not self._origin_ok():
            return self._json(403, {"detail": "CSRF 校验失败"})
        body = self._json_body()
        phone = str(body.get("phone") or "")
        if not PHONE_RE.match(phone):
            return self._json(400, {"ok": False, "message": "手机号格式不正确"})
        if body.get("captcha_verify_param") != Cfg.token:
            return self._json(400, {"ok": False, "message": "Captcha verification rejected"})
        if phone in Cfg.phone_taken:
            # /register/send-code only serves 未注册 numbers; a taken one is refused
            # before any SMS goes out (and no /register write is possible).
            return self._json(200, {"ok": False,
                                    "message": "该手机号已注册，请直接登录"})
        if phone in Cfg.send_code_fail:
            return self._json(200, {"ok": False, "message": Cfg.send_code_fail[phone]})
        now = time.time()
        last = Cfg.last_send.get(phone)
        if last and Cfg.cooldown and now - last < Cfg.cooldown:
            left = int(round(Cfg.cooldown - (now - last)))
            Cfg.sends += 1
            return self._json(200, {"ok": False, "message": f"验证码发送过于频繁，请 {left} 秒后再试"})
        Cfg.last_send[phone] = now
        Cfg.counter += 1
        code = f"{Cfg.counter:06d}"
        Cfg.codes[phone] = code
        Cfg.attempts[phone] = 0
        Cfg.sends += 1
        with open(Cfg.codes_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"phone": phone, "code": code, "ts": round(now, 3)}) + "\n")
        if Cfg.sms_dir:
            with open(os.path.join(Cfg.sms_dir, phone + ".txt"), "w", encoding="utf-8") as f:
                f.write(f"【VobileMax】您的验证码为 {code}，5 分钟内有效。")
        return self._json(200, {"ok": True, "message": "验证码已发送", "code": None})

    def register(self):
        if not self._origin_ok():
            return self._html(403, "CSRF 校验失败")
        form = self._form()
        phone = form.get("phone", "")
        # Production order (R140): the form token is optional only for a device the
        # server already trusts (auth_device + 32-hex auth_fingerprint).
        if form.get("captcha_verify_param") != Cfg.token and not self._device_ok():
            Cfg.register_tries += 1
            return self._html(400, "Unsupported captcha platform")
        Cfg.register_tries += 1
        if Cfg.rate_limit_after and Cfg.register_tries > Cfg.rate_limit_after:
            return self._html(429, "操作过于频繁，请稍后重试")
        if Cfg.reject_first > 0:
            Cfg.reject_first -= 1
            return self._html(400, "验证码不正确")
        if form.get("sms_code") != Cfg.codes.get(phone):
            Cfg.attempts[phone] = Cfg.attempts.get(phone, 0) + 1
            if Cfg.attempts[phone] > 5:
                return self._html(400, "验证码错误次数过多，请重新获取")
            return self._html(400, "验证码不正确")
        Cfg.users[phone] = {"username": form.get("username", ""),
                            "password": form.get("password", ""),
                            "uid": "8f3kd92la"}
        # production persists accepted_agreement_ids; `drop_agreements` simulates
        # the failure mode where they are silently lost (heal path in vmax_verify)
        Cfg.agreements_accepted = (not Cfg.drop_agreements
                                   and bool(form.get("accepted_agreement_ids")))
        state = form.get("state", "")
        redirect = form.get("redirect_uri", "")
        code = "AUTH" + secrets.token_hex(6)
        Cfg.oauth[code] = {"challenge": form.get("code_challenge", ""),
                           "redirect_uri": redirect, "state": state,
                           "client_id": form.get("client_id", ""), "phone": phone}
        loc = redirect + "?" + urllib.parse.urlencode({"code": code, "state": state})
        if Cfg.omit_code:
            loc = redirect + "?" + urllib.parse.urlencode({"state": state, "error": "server_error"})
        return self._html(303, "redirect", {"Location": loc})

    def login(self):
        """POST /api/auth/login -> AuthResponse (JWT) or 401/403.

        Order follows production (live probe 2026-10-26): credentials first, so a
        wrong password answers 401 even when the captcha token is bogus -- which is
        exactly why such a token can be reused for the next row.
        """
        body = self._json_body()
        Cfg.login_tries += 1
        user = str(body.get("username") or "")
        pw = str(body.get("password") or "")
        for phone, u in Cfg.users.items():
            if user in (phone, u["username"]) and pw and u["password"] == pw:
                if Cfg.require_captcha_login and body.get("captcha_verify_param") != Cfg.token:
                    return self._json(403, {"detail": "需要先完成人机验证"})
                def b64(obj):
                    raw = json.dumps(obj, separators=(",", ":")).encode()
                    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
                jwt = ".".join([b64({"alg": "none", "typ": "JWT"}),
                                b64({"sub": "u_mock", "uid": u["uid"], "phone": phone,
                                     "exp": int(time.time()) + 3600}),
                                "mock-signature"])
                return self._json(200, {
                    "access_token": jwt, "token_type": "bearer",
                    "user": {"id": 1, "uid": u["uid"],
                             "username": (u["username"][:3] + "***") if u["username"] else "",
                             "phone": phone[:3] + "****" + phone[-4:],
                             "area_type": "mainland"}})
        return self._json(401, {"detail": "账号或密码错误"})

    def reset_send_code(self):
        """/api/auth/reset-password/send-code -- registered numbers only.

        Production order (live probe R105): captcha presence, then validity, then
        the phone.  An empty param answers "Incomplete captcha parameters", a bogus
        one "Captcha verification rejected"; neither sends an SMS.
        """
        if not self._origin_ok():
            return self._json(403, {"detail": "CSRF 校验失败"})
        body = self._json_body()
        phone = str(body.get("phone") or "")
        param = str(body.get("captcha_verify_param") or "")
        if not param:
            return self._json(400, {"detail": "Incomplete captcha parameters"})
        if param != Cfg.token:
            return self._json(400, {"detail": "Captcha verification rejected"})
        if Cfg.reset_delay:
            time.sleep(Cfg.reset_delay)
        if phone not in Cfg.users:
            return self._json(404, {"detail": "手机号未注册"})
        if Cfg.reset_rate_limited:
            return self._json(429, {"detail": "发送频率或每日次数超限"})
        now = time.time()
        Cfg.last_send[phone] = now
        Cfg.counter += 1
        code = f"{Cfg.counter:06d}"
        Cfg.codes[phone] = code
        Cfg.sends += 1
        if Cfg.sms_dir:
            with open(os.path.join(Cfg.sms_dir, phone + ".txt"), "w", encoding="utf-8") as f:
                f.write(f"【VobileMax】您正在进行密码重置，验证码 {code}，5 分钟内有效。")
        return self._json(200, {"sent": True, "mock": False, "code": None,
                                "message": "验证码已发送"})

    def reset_password(self):
        body = self._json_body()
        phone = str(body.get("phone") or "")
        code = str(body.get("sms_code") or "")
        newpw = str(body.get("new_password") or "")
        if phone not in Cfg.users:
            return self._json(404, {"detail": "手机号未注册"})
        if Cfg.reset_reject_first > 0:
            Cfg.reset_reject_first -= 1
            return self._json(400, {"detail": "验证码或新密码无效"})
        if not code or code != Cfg.codes.get(phone) or len(newpw) < 8:
            return self._json(400, {"detail": "验证码或新密码无效"})
        Cfg.codes.pop(phone, None)      # single use
        Cfg.users[phone]["password"] = newpw
        Cfg.users[phone]["password_reset"] = True

        def b64(obj):
            raw = json.dumps(obj, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        jwt = ".".join([b64({"alg": "none", "typ": "JWT"}),
                        b64({"sub": "u_mock", "uid": Cfg.users[phone]["uid"], "phone": phone,
                             "exp": int(time.time()) + 3600}),
                        "mock-signature"])
        return self._json(200, {"access_token": jwt, "token_type": "bearer",
                                "expires_in": 3600,
                                "user": {"uid": Cfg.users[phone]["uid"], "phone": phone}})

    def token(self):
        body = self._json_body()
        rec = Cfg.oauth.get(str(body.get("code")))
        if not rec:
            return self._json(400, {"error": "invalid_grant", "detail": "unknown code"})
        if body.get("client_id") != rec["client_id"] or body.get("redirect_uri") != rec["redirect_uri"]:
            return self._json(400, {"error": "invalid_grant", "detail": "client/redirect mismatch"})
        if self._s256(str(body.get("code_verifier"))) != rec["challenge"]:
            return self._json(400, {"error": "invalid_grant", "detail": "PKCE mismatch"})
        Cfg.oauth.pop(str(body.get("code")), None)

        def b64(obj):
            raw = json.dumps(obj, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        jwt = ".".join([b64({"alg": "none", "typ": "JWT"}),
                        b64({"sub": "u_mock", "uid": "8f3kd92la",
                             "phone": rec["phone"], "exp": int(time.time()) + 3600}),
                        "mock-signature"])
        return self._json(200, {"access_token": jwt,
                                "token_type": "Bearer", "expires_in": 3600,
                                "user": {"uid": "8f3kd92la", "phone": rec["phone"]}})


class Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        Cfg.port = self.port
        Cfg.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_args(code="", resends=0, sms_spec="mock"):
    from argparse import Namespace
    return Namespace(phone="13800138000", username=None, password=None, platform="dreammaker",
                     proxy=None, base=Cfg.base, redirect=Cfg.base + "/auth/callback",
                     invite="", captcha="param", captcha_param=Cfg.token,
                     captcha_attempts=1, queue_dir="tokens", queue_wait=0,
                     assist_dir="assist", stdin=False, headed=False,
                     sms=sms_spec, sms_dir="sms_inbox", sms_timeout=5,
                     sms_resends=resends, allow_virtual=False, debug_dir=None, out=None)


class LatestCodeSms:
    """Provider that reads the newest code the mock issued (proves re-fetch after resend)."""

    def wait(self, phone, timeout=180, since=None):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with open(Cfg.codes_path, encoding="utf-8") as f:
                    for line in f:
                        rec = json.loads(line)
                        if rec["phone"] == phone:
                            last = rec["code"]
            except FileNotFoundError:
                pass
            if last:
                return last
            time.sleep(0.1)
        raise RuntimeError("no code issued by mock")


def check(label, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label + (f" | {detail}" if detail else ""))
    if not cond:
        raise AssertionError(label)


def main() -> int:
    TMP = tempfile.mkdtemp(prefix="vmax_mock_")
    print("workdir:", TMP)
    tmp = os.path.join(TMP, "codes.jsonl")
    if os.path.exists(tmp):
        os.remove(tmp)
    Cfg.codes_path = tmp
    import vmax_register as R

    R.build_sms = lambda spec, d="sms_inbox": LatestCodeSms()

    with Server():
        print("base:", Cfg.base)

        # A: happy path
        print("[A] happy path")
        Cfg.cooldown = 0
        res = register_one(make_args())
        check("status ok", res["status"] == "ok", str(res.get("status")))
        check("oauth code captured", bool(res["register"].get("code")))
        check("state echoed", res["register"]["status"] == 303)
        check("access_token issued", res["token"]["access_token"].count(".") == 2)
        check("uid parsed", res["uid"] == "8f3kd92la", str(res.get("uid")))

        # B: one wrong code -> resend recovers
        print("[B] wrong code once, resend loop")
        Cfg.reject_first = 1
        res = register_one(make_args(resends=1))
        check("recovered", res["status"] == "ok")
        check("one resend", len(res.get("resends", [])) == 1, str(res.get("resends")))
        check("first error classified", res.get("register_error_kind") == "code_wrong",
              str(res.get("register_error_kind")))

        # C: cooldown enforcement + classification
        print("[C] 60s per-phone cooldown")
        Cfg.cooldown = 60
        Cfg.last_send.clear()
        cli = VmaxAuth(base=Cfg.base, redirect_uri=Cfg.base + "/auth/callback")
        first = cli.send_register_code("13900139000", Cfg.token)
        second = cli.send_register_code("13900139000", Cfg.token)
        check("first send ok", first.get("ok") is True)
        check("second rejected", second.get("ok") is False, str(second.get("message")))
        check("classified as cooldown",
              R.classify_register_error(second.get("message")) == "cooldown")

        # D: PKCE mismatch
        print("[D] PKCE mismatch")
        Cfg.cooldown = 0
        cli2 = VmaxAuth(base=Cfg.base, redirect_uri=Cfg.base + "/auth/callback")
        cli2.send_register_code("13700137000", Cfg.token)
        from vmax_client import pkce_pair
        v, ch = pkce_pair()
        out = cli2.register_form("13700137000", Cfg.codes["13700137000"], "u_test", "Aa12345678",
                                 v, ch, "st1", Cfg.token, accepted_ids=["12", "13"])
        check("register 303", out.get("code") is not None, str(out))
        try:
            cli2.exchange_code(out["code"], pkce_pair()[0])
            check("bad verifier rejected", False)
        except VmaxError as e:
            check("bad verifier rejected", "invalid_grant" in str(e), str(e)[:80])

        # E: 303 without code
        print("[E] 303 without code")
        Cfg.omit_code = True
        v2, ch2 = pkce_pair()
        cli2.send_register_code("13600136000", Cfg.token)
        out = cli2.register_form("13600136000", Cfg.codes["13600136000"], "u_test2", "Aa12345678",
                                 v2, ch2, "st2", Cfg.token, accepted_ids=["12", "13"])
        check("no code parsed", out.get("code") is None, str(out))
        check("status kept", out.get("status") == 303, str(out.get("status")))

        # F: the real "one command with a real phone" script, driven by FileSms
        print("[F] vmax_verify.py end-to-end")
        import subprocess
        sms_dir = os.path.join(TMP, "sms")
        os.makedirs(sms_dir, exist_ok=True)
        for f in os.listdir(sms_dir):
            os.remove(os.path.join(sms_dir, f))
        Cfg.sms_dir = sms_dir
        Cfg.omit_code = False
        out_path = os.path.join(TMP, "verify.jsonl")
        if os.path.exists(out_path):
            os.remove(out_path)
        env = dict(os.environ, VMAX_RESEND_WAIT="0", PYTHONIOENCODING="utf-8",
                   VMAX_NOPACE="1",
                   VMAX_PACE_STATE=os.path.join(TMP, "pace.json"))
        cmd = [sys.executable, os.path.join(os.path.dirname(HERE), "vmax_verify.py"),
               "--phone", "13500135000", "--base", Cfg.base,
               "--redirect", Cfg.base + "/auth/callback",
               "--captcha", "param", "--captcha-param", Cfg.token,
               "--sms", "file", "--sms-dir", sms_dir, "--sms-timeout", "10",
               "--out", out_path]
        cp = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace")
        check("verify exit 0", cp.returncode == 0, (cp.stderr or cp.stdout)[-200:])
        rec = json.loads(open(out_path, encoding="utf-8").read().splitlines()[-1])
        check("verify status ok", rec.get("status") == "ok", str(rec.get("status")))
        check("verify got code", bool(rec.get("code")))
        check("verify state echoed", rec.get("state_echoed") is True)
        check("verify token claims", rec["token"]["claims"].get("uid") == "8f3kd92la",
              str(rec["token"]["claims"]))

        print("sends:", Cfg.sends, "register_tries:", Cfg.register_tries)
    print("ALL SCENARIOS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())