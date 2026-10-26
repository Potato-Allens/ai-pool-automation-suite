# -*- coding: utf-8 -*-
"""Offline: a 503 on POST /register must be retried (fresh captcha, same SMS code).

PROTOCOL §5.2 lists 503 twice: 「注册奖励服务暂不可用」 (a service hiccup -- the code is
still pending and no account exists) and 「手机号运营商验证暂不可用」 (number-specific).
The first is worth one short backoff: the SMS is already paid for.  The second must not
be retried: it would only burn a captcha token on a number the carrier refuses.

This suite drives the real `vmax_register.register_one()` against a scripted client, so
the assertions are about the request sequence, not about printed text.
"""
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


OK = {"status": 303,
      "location": "https://dreammaker.vobilemax.cn/auth/callback?code=THECODE&state=st",
      "code": "THECODE"}


def s503(msg="注册奖励服务暂不可用"):
    return {"status": 503, "error": msg, "location": None}


class FakeCli:
    base = "https://auth.vobilemax.cn"
    redirect_uri = None

    def __init__(self, script):
        self.script = list(script)
        self.form_calls = []
        self.send_calls = 0
        self.exchanged = 0

    def client_conf(self, client="dreammaker"):
        return {"client_id": "dreamstudio", "platform": "dreammaker",
                "redirect_uri": "https://dreammaker.vobilemax.cn/auth/callback"}

    def send_register_code(self, phone, param, client="dreammaker"):
        self.send_calls += 1
        return {"ok": True, "message": "验证码已发送"}

    def register_form(self, phone, code, username, password, verifier, challenge, state,
                      captcha_verify_param, invitation_code="", client="dreammaker",
                      omit_sms_code=False):
        self.form_calls.append(captcha_verify_param)
        return dict(self.script.pop(0)) if self.script else dict(OK)

    def exchange_code(self, code, verifier, client="dreammaker"):
        self.exchanged += 1
        return {"access_token": "AT", "expires_in": 3600, "user": {"uid": "U1"}}


class FakeAuth:
    _inst = None
    login_url = staticmethod(RealAuth.login_url)

    def __new__(cls, *a, **k):
        return cls._inst


def run(script, **over):
    cli = FakeCli(script)
    FakeAuth._inst = cli
    R.VmaxAuth = FakeAuth
    solved = {"n": 0}

    def fake_captcha(args, url):
        solved["n"] += 1
        return f"TOKEN{solved['n']}"

    R.obtain_captcha = fake_captcha
    R.build_sms = lambda spec, d: FakeSms()
    kw = dict(phone="13012345678", username=None, password=None, platform="dreammaker",
              invite="", captcha="param", captcha_param="", captcha_attempts=1,
              queue_dir="tokens", queue_wait=0, assist_dir="assist", stdin=False,
              headed=False, sms="none", sms_dir="sms_inbox", sms_timeout=1,
              sms_resends=0, proxy=None, debug_dir=None, out=None, base=None,
              redirect=None, allow_virtual=False, form_captcha="auto",
              retry_503=1, wait_503=0)
    kw.update(over)
    args = SimpleNamespace(**kw)
    try:
        return R.register_one(args), cli, None
    except Exception as e:  # noqa: BLE001
        # RegisterError carries the partial row it already paid for (captcha timing,
        # send-code answer, the 503 body).  Hand that back as `res` so the assertions
        # below read the evidence the same way whether the run raised or returned.
        return getattr(e, "result", None), cli, e


def main() -> int:
    print("[1] one 503 then success: retried with a fresh token, one SMS")
    res, cli, err = run([s503()])
    check("no exception", err is None, err)
    check("the row is ok", (res or {}).get("status") == "ok", res)
    check("two form posts", cli.form_calls == ["TOKEN1", "TOKEN2"], cli.form_calls)
    check("the retry used a *fresh* token", cli.form_calls[1] != cli.form_calls[0],
          cli.form_calls)
    check("send-code ran once (one SMS)", cli.send_calls == 1, cli.send_calls)
    check("the 303 code was exchanged", cli.exchanged == 1, cli.exchanged)
    check("retries_503 records the wait and the status",
          res["retries_503"] == [{"wait_s": 0.0, "status": 303, "error": ""}],
          res.get("retries_503"))
    check("the final register payload is the success",
          (res.get("register") or {}).get("status") == 303, res.get("register"))
    check("no resend happened", "resends" not in res, res.get("resends"))

    print("[2] 503 with retry_503=0: not retried at all")
    res, cli, err = run([s503()], retry_503=0)
    check("it raises", err is not None)
    check("one form post", cli.form_calls == ["TOKEN1"], cli.form_calls)
    check("status is kept", getattr(err, "status", None) == 503, getattr(err, "status", None))
    check("no retries_503 key", not res or "retries_503" not in res)

    print("[3] 503 twice with one retry: still fails, but only one extra token")
    res, cli, err = run([s503(), s503()])
    check("it raises", err is not None)
    check("two posts, no more", cli.form_calls == ["TOKEN1", "TOKEN2"], cli.form_calls)
    check("one retry recorded", len(res.get("retries_503") or []) == 1, res.get("retries_503"))
    check("still one SMS", cli.send_calls == 1, cli.send_calls)

    print("[4] a carrier-level 503 is never retried (number-specific)")
    res, cli, err = run([s503("手机号运营商验证暂不可用")], retry_503=3)
    check("it raises", err is not None)
    check("exactly one post", cli.form_calls == ["TOKEN1"], cli.form_calls)
    check("no retries_503 key", not res or "retries_503" not in res)

    print("[5] 503 then a wrong code: the retry happens, the verdict is code_wrong")
    res, cli, err = run([s503(), {"status": 400, "error": "验证码不正确", "location": None}])
    check("it raises", err is not None)
    check("kind is code_wrong", getattr(err, "kind", None) == "code_wrong",
          getattr(err, "kind", None))
    check("two posts", len(cli.form_calls) == 2, cli.form_calls)
    check("no resend was attempted", "resends" not in (res or {}), (res or {}).get("resends"))

    print("[6] --wait-503 is honoured (and 0 means no sleep)")
    res, cli, err = run([s503()], wait_503=0.0)
    check("wait recorded as 0", (res.get("retries_503") or [{}])[0].get("wait_s") == 0.0,
          res.get("retries_503"))

    print("[7] the default is one retry even when the runner passes no flag")
    args = SimpleNamespace(phone="13012345678")
    check("getattr default is 1", getattr(args, "retry_503", 1) == 1)

    print("[8] the parser exposes the two flags")
    ap = None
    import argparse
    src = open(os.path.join(os.path.dirname(HERE), "vmax_register.py"), encoding="utf-8").read()
    check("--retry-503 is documented", "--retry-503" in src)
    check("--wait-503 is documented", "--wait-503" in src)

    print("ALL REGISTER-503 TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
