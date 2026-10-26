# -*- coding: utf-8 -*-
"""Offline: register_one must retry POST /register with a fresh captcha token when the
send-code token is refused (measured: Aliyun tokens are consumed by send-code)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vmax_register as R  # noqa: E402
from vmax_client import VmaxAuth as RealAuth  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


class FakeSms:
    def wait(self, phone, timeout=1, since=None):
        return "123456"


class FakeCli:
    base = "https://auth.vobilemax.cn"
    redirect_uri = None

    def __init__(self):
        self.form_calls = []
        self.send_calls = 0
        self.device_calls = 0
        self.device_answer = {"requires_captcha": False}
        self.device_error = None
        self.reject_empty_form = False
        self.empty_form_error = "Captcha verification rejected"

    def client_conf(self, client="dreammaker"):
        return {"client_id": "dreamstudio", "platform": "dreammaker",
                "redirect_uri": "https://dreammaker.vobilemax.cn/auth/callback"}

    def send_register_code(self, phone, param, client="dreammaker"):
        self.send_calls += 1
        return {"ok": True, "message": "验证码已发送"}

    def prepare_device(self, fingerprint=None, login_url=None):
        """Stands in for VmaxAuth.prepare_device (R140 device path)."""
        self.device_calls += 1
        if self.device_error is not None:
            raise self.device_error
        return dict(self.device_answer)

    def register_form(self, phone, code, username, password, verifier, challenge, state,
                      captcha_verify_param, invitation_code="", client="dreammaker"):
        self.form_calls.append(captcha_verify_param)
        if captcha_verify_param == "" and self.reject_empty_form:
            return {"status": 400, "error": self.empty_form_error,
                    "location": None}
        # TOKEN1 is the send-code token and is already spent; anything else is fresh.
        if captcha_verify_param == "TOKEN1":
            return {"status": 400, "error": "Captcha verification rejected", "location": None}
        return {"status": 303,
                "location": "https://dreammaker.vobilemax.cn/auth/callback?code=THECODE&state=st",
                "code": "THECODE"}

    def exchange_code(self, code, verifier, client="dreammaker"):
        return {"access_token": "AT", "expires_in": 3600, "user": {"uid": "U1"}}


class FakeAuth:
    """Stands in for VmaxAuth: constructor returns the shared fake instance."""
    _inst = None
    login_url = staticmethod(RealAuth.login_url)

    def __new__(cls, *a, **k):
        return cls._inst


def run(policy: str):
    cli = FakeCli()
    FakeAuth._inst = cli
    R.VmaxAuth = FakeAuth
    solved = {"n": 0}

    def fake_captcha(args, url):
        solved["n"] += 1
        return f"TOKEN{solved['n']}"

    R.obtain_captcha = fake_captcha
    R.build_sms = lambda spec, d: FakeSms()
    args = SimpleNamespace(
        phone="13012345678", username=None, password=None, platform="dreammaker", invite="",
        captcha="param", captcha_param="", captcha_attempts=1, queue_dir="tokens", queue_wait=0,
        assist_dir="assist", stdin=False, headed=False, sms="none", sms_dir="sms_inbox",
        sms_timeout=1, sms_resends=0, proxy=None, debug_dir=None, out=None, base=None,
        redirect=None, allow_virtual=False, form_captcha=policy,
    )
    try:
        res = R.register_one(args)
    except Exception as e:  # noqa: BLE001
        res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    return cli, solved, res


print("[1] auto: reuse, then re-solve on rejection")
cli, solved, res = run("auto")
check("register ok", res.get("status") == "ok", res)
check("two form calls", len(cli.form_calls) == 2, cli.form_calls)
check("second token differs", cli.form_calls[0] != cli.form_calls[1], cli.form_calls)
check("captcha solved twice", solved["n"] == 2, solved)
check("retry recorded", res.get("register", {}).get("captcha_retry") is True, res.get("register"))
check("policy recorded", res.get("form_captcha") == "auto_retry", res.get("form_captcha"))
check("send-code only once", cli.send_calls == 1, cli.send_calls)

print("[2] reuse: never spend a second token")
cli, solved, res = run("reuse")
check("fails without retry", res.get("status") == "error", res)
check("one form call", len(cli.form_calls) == 1, cli.form_calls)
check("one solve", solved["n"] == 1, solved)

print("[3] fresh: solve a token for the form up front")
cli, solved, res = run("fresh")
check("register ok", res.get("status") == "ok", res)
check("two solves", solved["n"] == 2, solved)
check("policy recorded", res.get("form_captcha") == "fresh", res.get("form_captcha"))

def run_case(policy, answer=None, error=None, reject_empty=False, empty_error=None):
    cli = FakeCli()
    if answer is not None:
        cli.device_answer = answer
    cli.device_error = error
    cli.reject_empty_form = reject_empty
    if empty_error:
        cli.empty_form_error = empty_error
    FakeAuth._inst = cli
    R.VmaxAuth = FakeAuth
    solved = {"n": 0}

    def fake_captcha(args, url):
        solved["n"] += 1
        return f"TOKEN{solved['n']}"

    R.obtain_captcha = fake_captcha
    R.build_sms = lambda spec, d: FakeSms()
    args = SimpleNamespace(
        phone="13012345678", username=None, password=None, platform="dreammaker", invite="",
        captcha="param", captcha_param="", captcha_attempts=1, queue_dir="tokens", queue_wait=0,
        assist_dir="assist", stdin=False, headed=False, sms="none", sms_dir="sms_inbox",
        sms_timeout=1, sms_resends=0, proxy=None, debug_dir=None, out=None, base=None,
        redirect=None, allow_virtual=False, form_captcha=policy,
    )
    try:
        res = R.register_one(args)
    except Exception as e:  # noqa: BLE001
        res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    return cli, solved, res


print("[4] device: the form needs no token at all")
cli, solved, res = run_case("device")
check("register ok", res.get("status") == "ok", res)
check("one form call", len(cli.form_calls) == 1, cli.form_calls)
check("the form got an empty captcha param", cli.form_calls == [""], cli.form_calls)
check("only send-code solved a token", solved["n"] == 1, solved)
check("device path recorded", (res.get("device") or {}).get("ok") is True, res.get("device"))
check("policy recorded as device", res.get("form_captcha") == "device", res.get("form_captcha"))
check("signup-check was asked once", cli.device_calls == 1, cli.device_calls)

print("[5] device refused by signup-check -> pay for the form token")
cli, solved, res = run_case("device", {"requires_captcha": True})
check("still registers", res.get("status") == "ok", res)
check("two solves (send-code + form)", solved["n"] == 2, solved)
check("form got the paid token", cli.form_calls == ["TOKEN2"], cli.form_calls)
check("policy recorded as fallback", res.get("form_captcha") == "device_fallback",
      res.get("form_captcha"))
check("device.ok is False", (res.get("device") or {}).get("ok") is False, res.get("device"))

print("[6] device trusted but /register still refuses -> buy one token and retry")
cli, solved, res = run_case("device", {"requires_captcha": False}, reject_empty=True)
check("registers after the retry", res.get("status") == "ok", res)
check("form called twice: empty, then a token", cli.form_calls == ["", "TOKEN2"], cli.form_calls)
check("retry recorded", (res.get("register") or {}).get("captcha_retry") is True,
      res.get("register"))
check("policy recorded as device_retry", res.get("form_captcha") == "device_retry",
      res.get("form_captcha"))

print("[7] device path errors out -> fall back without losing the card")
cli, solved, res = run_case("device", {}, error=RuntimeError("boom"))
check("still registers", res.get("status") == "ok", res)
check("one extra solve", solved["n"] == 2, solved)
check("form got the paid token", cli.form_calls == ["TOKEN2"], cli.form_calls)
check("the error is kept in the row", "boom" in str((res.get("device") or {}).get("error")),
      res.get("device"))

print("[8] device: the production wording is what triggers device_retry")
check("classify() knows the measured string",
      R.classify_register_error("Unsupported captcha platform") == "captcha_rejected",
      R.classify_register_error("Unsupported captcha platform"))
cli, solved, res = run_case("device", {"requires_captcha": False}, reject_empty=True,
                            empty_error="Unsupported captcha platform")
check("registers after the retry", res.get("status") == "ok", res)
check("form called twice: empty, then a token", cli.form_calls == ["", "TOKEN2"],
      cli.form_calls)
check("policy recorded as device_retry", res.get("form_captcha") == "device_retry",
      res.get("form_captcha"))
cli, solved, res = run_case("auto", reject_empty=True,
                            empty_error="Unsupported captcha platform")
check("auto re-solves the same way", res.get("status") == "ok", res)
check("auto_retry recorded", res.get("form_captcha") == "auto_retry",
      res.get("form_captcha"))

print("ALL FORM-CAPTCHA TESTS PASS" if not fails else f"FAILURES: {fails}")
sys.exit(0 if not fails else 1)
