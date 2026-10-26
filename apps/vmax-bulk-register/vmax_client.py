# -*- coding: utf-8 -*-
"""auth.vobilemax.cn (SeeBay 统一认证 / Auth Console) protocol client.

Reverse engineered 2026-10-26. Pure HTTP, no browser needed except for the
Aliyun INPAITING captcha (see vmax_captcha.py).

Flow (register + OAuth code):
    verifier, challenge = pkce_pair()
    cli = VmaxAuth()
    cli.send_register_code(phone, captcha_verify_param)
    cli.register_form(phone, sms_code, username, password, verifier, challenge)
        -> {"code": "...", "location": "https://dreammaker.../?code=..."}
    cli.exchange_code(code, verifier) -> {"access_token": "...", "expires_in": ...}
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests

BASE = "https://auth.vobilemax.cn"
CLIENT_ID = "dreamstudio"
REDIRECT_URI = "https://dreammaker.vobilemax.cn/auth/callback"
CLIENTS = {
    "dreammaker": {"client_id": "dreamstudio", "redirect_uri": REDIRECT_URI, "platform": "dreammaker"},
    "max": {"client_id": "max", "redirect_uri": "https://max.vobilemax.cn/auth/callback", "platform": "max"},
}
# Test environment confirmed 2026-10-26: same openapi (41 paths), same client_id,
# only redirect_uri whitelist differs. Agreement ids are 11/12 instead of 12/13.
TEST_BASE = "https://test-auth.vobilemax.cn"
TEST_REDIRECT_URI = "https://test-dreammaker.vobilemax.cn/auth/callback"
PROFILES = {
    "prod": {"base": BASE, "redirect_uri": None},
    "test": {"base": TEST_BASE, "redirect_uri": TEST_REDIRECT_URI},
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")


class VmaxError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def pkce_pair(nbytes: int = 48) -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(nbytes)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _parse_error_page(body: str) -> Optional[str]:
    m = re.search(r'id="login-error"[^>]*>(.*?)</div>', body, re.S)
    if m:
        return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1)))).strip()
    return None


class VmaxAuth:
    def __init__(self, proxy: Optional[str] = None, timeout: int = 25, ua: str = UA,
                 base: Optional[str] = None, redirect_uri: Optional[str] = None):
        # base/redirect default from env so the CLI flags only need to be passed once.
        self.base = (base or os.environ.get("VMAX_BASE") or BASE).rstrip("/")
        self.redirect_uri = redirect_uri or os.environ.get("VMAX_REDIRECT_URI") or None
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": ua,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        # Register writes share one IP-scoped bucket (docs/PROTOCOL.md S7.2).
        # VMAX_NOPACE=1 disables client-side pacing (single manual probe).
        self.pace_key = "register@" + (proxy or "direct")
        self.pacer = None
        if not os.environ.get("VMAX_NOPACE"):
            try:
                from vmax_pacer import Pacer
                self.pacer = Pacer()
            except Exception:  # noqa: BLE001 - pacing is best effort
                self.pacer = None

    # ---------------- discovery ----------------
    def captcha_config(self, platform: str = "dreammaker") -> dict:
        r = self.s.get(f"{self.base}/api/auth/captcha-config", params={"platform": platform}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def agreements(self, types: str = "terms,privacy") -> list:
        r = self.s.get(f"{self.base}/api/auth/agreements", params={"types": types}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def agreement_ids(self) -> list[str]:
        return [str(a["id"]) for a in self.agreements()]

    def sms_config(self) -> dict:
        r = self.s.get(f"{self.base}/api/auth/sms-config", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def session(self) -> dict:
        r = self.s.get(f"{self.base}/session", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def agreement_status(self, types: str = "terms,privacy") -> list:
        """What *this session* has signed, per currently active version.

        Session-cookie auth (no bearer): it only answers after a login or a
        registration, which is exactly when we want to check that the
        ``accepted_agreement_ids`` we posted were actually persisted.
        """
        r = self.s.get(f"{self.base}/api/auth/agreements/my-status",
                       params={"types": types}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def csrf_token(self) -> str:
        """The session CSRF token: `__Host-auth_csrf` cookie first, /session second.

        Live schema for the accept endpoint (2026-10-26, see
        evidence/_r128_agreements_gate.log) requires BOTH the cookie and the
        `X-CSRF-Token` header next to an Origin header, so a bare POST is 403.
        """
        token = (self.s.cookies.get_dict() or {}).get("__Host-auth_csrf", "")
        if token:
            return token
        try:
            return str((self.session() or {}).get("csrf_token") or "")
        except Exception:  # noqa: BLE001 - caller decides what a missing token means
            return ""

    def accept_agreements(self, ids, context: str = "vmax",
                          csrf_token: str = "") -> dict:
        """Sign the active versions for the current session (idempotent enough).

        Used only as a repair step when ``agreement_status`` says the versions
        we posted at registration did not stick.
        """
        token = csrf_token or self.csrf_token()
        payload = {"ids": [int(i) for i in ids], "context": context}
        headers = {"Origin": self.base, "Referer": f"{self.base}/login"}
        if token:
            headers["X-CSRF-Token"] = token
        r = self.s.post(f"{self.base}/api/auth/agreements/accept", json=payload,
                        headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ---------------- auth URLs ----------------
    def client_conf(self, client: str = "dreammaker") -> dict:
        """Client id / redirect for the current base (redirect override wins)."""
        c = dict(CLIENTS[client])
        if self.redirect_uri:
            c["redirect_uri"] = self.redirect_uri
        return c

    @staticmethod
    def login_url(challenge: str, state: Optional[str] = None, client: str = "dreammaker",
                  base: Optional[str] = None, redirect_uri: Optional[str] = None) -> str:
        from urllib.parse import urlencode
        c = dict(CLIENTS[client])
        if redirect_uri:
            c["redirect_uri"] = redirect_uri
        base = (base or os.environ.get("VMAX_BASE") or BASE).rstrip("/")
        q = {
            "response_type": "code",
            "client_id": c["client_id"],
            "redirect_uri": c["redirect_uri"],
            "state": state or secrets.token_urlsafe(16),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "locale": "zh-CN",
        }
        return f"{base}/login?{urlencode(q)}"

    # ---------------- SMS ----------------
    def signup_check(self) -> dict:
        """GET /api/auth/signup-check -- the widget asks this before submitting.

        Not in openapi (front-end private route).  ``{"requires_captcha": true}``
        means the next ``POST /register`` needs a real token; ``false`` means the
        server trusts this device and the form is accepted with an empty
        ``captcha_verify_param`` (measured 2026-10-26, see PROTOCOL 5.2.4).
        """
        r = self.s.get(f"{self.base}/api/auth/signup-check", timeout=self.timeout)
        try:
            body = r.json()
        except ValueError:
            raise VmaxError(f"signup-check: non-JSON {r.status_code}", r.status_code,
                            r.text[:300])
        if r.status_code >= 400 or not isinstance(body, dict):
            raise VmaxError(f"signup-check: HTTP {r.status_code} {body!r}",
                            r.status_code, r.text[:300])
        return body

    def prepare_device(self, fingerprint: str | None = None,
                       login_url: str | None = None, locale: str = "zh-CN") -> dict:
        """Walk the site's own captcha-free path, then report what it decided.

        Order matters and was measured live (R140): ``GET /login`` first -- that
        is what sets ``auth_device`` -- and only then the ``auth_fingerprint``
        cookie.  Fingerprint alone answers ``requires_captcha: true``; the site's
        own signup-device.js does exactly this, with FingerprintJS' 32-hex
        visitorId and a 30-day Max-Age.

        Returns the signup-check body.  Raises only on transport / protocol
        errors, so a caller can fall back to a paid token.
        """
        fp = fingerprint or secrets.token_hex(16)
        if not re.fullmatch(r"[0-9a-f]{32}", str(fp or "")):
            raise VmaxError("device fingerprint must be 32 lowercase hex chars")
        host = urlparse(self.base).hostname or ""
        r = self.s.get(login_url or f"{self.base}/login", timeout=self.timeout)
        # R142: a bare /login (no query string) answers 422 yet *still* sets
        # ``auth_device``, and the trust works -- measured with the cookie the
        # 422 handed out, signup-check answered false.  So the status alone is
        # not the gate: if the cookie landed, carry on and let signup-check
        # decide.  Without the cookie a 4xx is fatal, because the next call
        # would be a guaranteed "requires_captcha: true" (one wasted token).
        if r.status_code >= 400 and "auth_device" not in self.s.cookies.get_dict():
            raise VmaxError(f"device: GET /login -> {r.status_code}", r.status_code,
                            r.text[:200])
        self.s.cookies.set("auth_fingerprint", fp, domain=host, path="/")
        body = self.signup_check()
        body["_fingerprint"] = fp
        return body

    def send_register_code(self, phone: str, captcha_verify_param: str,
                           locale: str = "zh-CN", client: str = "dreammaker") -> dict:
        """POST /register/send-code -- same-origin check on the Origin header."""
        body = {
            "phone": phone,
            "client_id": self.client_conf(client)["client_id"],
            "locale": locale,
            "captcha_verify_param": captcha_verify_param,
        }
        r = self.s.post(f"{self.base}/register/send-code", json=body,
                        headers={"Origin": self.base, "Referer": f"{self.base}/login"},
                        timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            raise VmaxError(f"send-code: non-JSON {r.status_code}", r.status_code, r.text[:400])
        if r.status_code == 429:
            raise VmaxError("send-code: rate limited (429)", 429, data)
        if r.status_code >= 400:
            raise VmaxError(f"send-code: HTTP {r.status_code} {data}", r.status_code, data)
        return data

    def send_login_code(self, phone: str, captcha_verify_param: str, locale: str = "zh-CN",
                        client: str = "dreammaker") -> dict:
        body = {"phone": phone, "client_id": self.client_conf(client)["client_id"],
                "locale": locale, "captcha_verify_param": captcha_verify_param}
        r = self.s.post(f"{self.base}/login/send-code", json=body,
                        headers={"Origin": self.base}, timeout=self.timeout)
        return r.json() if r.content else {}

    # ---------------- register ----------------
    def register_form(self, phone: str, sms_code: str, username: str, password: str,
                      verifier: str, challenge: str, state: str,
                      captcha_verify_param: str, accepted_ids: Optional[list[str]] = None,
                      invitation_code: str = "", client: str = "dreammaker",
                      omit_sms_code: bool = False) -> dict:
        """POST /register (the HTML form). Returns {"status", "location", "code", "error"}."""
        c = self.client_conf(client)
        if accepted_ids is None:
            accepted_ids = self.agreement_ids()
        data: list[tuple[str, str]] = [
            ("response_type", "code"),
            ("client_id", c["client_id"]),
            ("redirect_uri", c["redirect_uri"]),
            ("state", state),
            ("code_challenge", challenge),
            ("code_challenge_method", "S256"),
            ("locale", "zh-CN"),
            ("username", username),
            ("phone", phone),
            ("sms_code", sms_code),
            ("password", password),
            ("confirm_password", password),
            ("invitation_code", invitation_code),
            ("captcha_platform", c["platform"]),
            ("captcha_verify_param", captcha_verify_param),
        ]
        data += [("accepted_agreement_ids", i) for i in accepted_ids]
        if omit_sms_code:
            # R133 / PROTOCOL §5.2.2: a code is pending, but the form does not carry
            # the field at all (dropping it is not the same as sending "").  Only this
            # variant answers "is sms_code actually required by the handler?".
            data = [kv for kv in data if kv[0] != "sms_code"]
        if self.pacer is not None:
            self.pacer.acquire(self.pace_key)
        r = self.s.post(f"{self.base}/register", data=data,
                        headers={"Origin": self.base, "Referer": f"{self.base}/login"},
                        allow_redirects=False, timeout=self.timeout)
        if self.pacer is not None:
            self.pacer.on_response(r.status_code, self.pace_key)
        out: dict[str, Any] = {"status": r.status_code, "location": r.headers.get("location")}
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("location", "")
            m = re.search(r"[?&]code=([^&]+)", loc)
            out["code"] = m.group(1) if m else None
        else:
            out["error"] = _parse_error_page(r.text) or r.text[:200]
        return out

    def register_json(self, phone: str, sms_code: str, username: str, password: str,
                      captcha_verify_param: str, platform: str = "dreammaker",
                      invitation_code: str = "", email: str = "") -> dict:
        body = {
            "captcha_platform": platform,
            "captcha_verify_param": captcha_verify_param,
            "username": username,
            "password": password,
            "invitation_code": invitation_code,
            "phone": phone,
            "sms_code": sms_code,
            "email": email,
        }
        if self.pacer is not None:
            self.pacer.acquire(self.pace_key)
        r = self.s.post(f"{self.base}/api/auth/register", json=body, timeout=self.timeout)
        if self.pacer is not None:
            self.pacer.on_response(r.status_code, self.pace_key)
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:300]}
        if r.status_code >= 400:
            raise VmaxError(f"register: HTTP {r.status_code} {data}", r.status_code, data)
        return data

    # ---------------- password reset (recover an existing account) ----------------
    def send_reset_code(self, phone: str, captcha_verify_param: str, locale: str = "zh-CN",
                        client: str = "dreammaker") -> dict:
        """POST /api/auth/reset-password/send-code -- registered numbers only.

        Returns the parsed body plus ``status``/``ok`` instead of raising: 404
        (not registered) and 429 (daily cap) are *answers* to route on, exactly
        like ``/register/send-code``'s ``ok:false``.
        """
        body = {"phone": phone, "captcha_platform": self.client_conf(client)["platform"],
                "captcha_verify_param": captcha_verify_param}
        r = self.s.post(f"{self.base}/api/auth/reset-password/send-code", json=body,
                        headers={"Origin": self.base, "Referer": f"{self.base}/login"},
                        timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:300]}
        out = dict(data) if isinstance(data, dict) else {"detail": str(data)[:300]}
        out["status"] = r.status_code
        out["ok"] = r.status_code < 400 and bool(out.get("sent", "detail" not in out))
        out.setdefault("message", out.get("detail") or "")
        return out

    def reset_password(self, phone: str, sms_code: str, new_password: str) -> dict:
        """POST /api/auth/reset-password -> AuthResponse (a JWT straight away).

        No captcha on this step (openapi ResetPasswordRequest carries only
        phone / sms_code / new_password), so a spent token can never strand a
        delivered SMS.
        """
        body = {"phone": phone, "sms_code": sms_code, "new_password": new_password}
        r = self.s.post(f"{self.base}/api/auth/reset-password", json=body,
                        headers={"Origin": self.base, "Referer": f"{self.base}/login"},
                        timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:300]}
        if r.status_code >= 400:
            raise VmaxError(f"reset-password: HTTP {r.status_code} {data}", r.status_code, data)
        return data

    # ---------------- login ----------------
    def login_sms(self, phone: str, sms_code: str, captcha_verify_param: str) -> dict:
        body = {"phone": phone, "sms_code": sms_code,
                "captcha_platform": "dreammaker", "captcha_verify_param": captcha_verify_param}
        r = self.s.post(f"{self.base}/api/auth/login-sms", json=body, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:300]}
        if r.status_code >= 400:
            raise VmaxError(f"login-sms: HTTP {r.status_code} {data}", r.status_code, data)
        return data

    # ---------------- OAuth ----------------
    def login_password(self, username: str, password: str,
                       captcha_verify_param: str = "", platform: str = "dreammaker") -> dict:
        """POST /api/auth/login -- password login, returns a JWT (AuthResponse).

        Openapi: 401 = wrong credentials, 403 = captcha required.  The answer is
        returned (not raised) with a ``status`` key so callers can classify it;
        one captcha token is spent per attempt because the captcha is checked
        before the password.
        """
        body = {"username": username, "password": password,
                "captcha_platform": self.client_conf(platform)["platform"],
                "captcha_verify_param": captcha_verify_param}
        r = self.s.post(f"{self.base}/api/auth/login", json=body,
                        headers={"Origin": self.base, "Referer": f"{self.base}/login"},
                        timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:300]}
        if not isinstance(data, dict):
            data = {"detail": str(data)[:300]}
        data["status"] = r.status_code
        return data

    def exchange_code(self, code: str, verifier: str, client: str = "dreammaker") -> dict:
        c = self.client_conf(client)
        body = {"grant_type": "authorization_code", "code": code,
                "client_id": c["client_id"], "redirect_uri": c["redirect_uri"],
                "code_verifier": verifier}
        r = self.s.post(f"{self.base}/api/auth/oauth/token", json=body, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:300]}
        if r.status_code >= 400:
            raise VmaxError(f"token: HTTP {r.status_code} {data}", r.status_code, data)
        return data

    def authorize_with_session(self, verifier: str, challenge: str, state: str,
                               client: str = "dreammaker") -> Optional[str]:
        """GET /authorize using the cookie session; returns the auth code if redirected."""
        c = self.client_conf(client)
        from urllib.parse import urlencode
        q = {"response_type": "code", "client_id": c["client_id"], "redirect_uri": c["redirect_uri"],
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
             "locale": "zh-CN"}
        r = self.s.get(f"{self.base}/authorize?{urlencode(q)}", allow_redirects=False, timeout=self.timeout)
        loc = r.headers.get("location", "")
        m = re.search(r"[?&]code=([^&]+)", loc)
        return m.group(1) if m else None
