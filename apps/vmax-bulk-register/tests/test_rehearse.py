# -*- coding: utf-8 -*-
"""Offline checks for the rehearsal verdict mapping (no network)."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vmax_rehearse as R  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def main():
    print("[1] probe message mapping")
    cases = [
        ("手机号格式不正确", "token_accepted"),
        ("Captcha verification rejected", "token_rejected"),
        ("Unsupported captcha platform", "token_missing"),
        ("验证码发送过于频繁，请 59 秒后再试", "cooldown"),
        ("something else", "other"),
        ("", "other"),
    ]
    for msg, want in cases:
        got = R.classify_probe_message(msg)
        check(f"{msg[:28]!r} -> {want}", got == want, got)

    print("[2] form result mapping")
    forms = [
        ({"status": 400, "error": "请先获取短信验证码"}, "reached_sms_gate"),
        ({"status": 400, "error": "Captcha verification rejected"}, "token_rejected"),
        ({"status": 400, "error": "Unsupported captcha platform"}, "token_missing"),
        ({"status": 400, "error": "手机号格式不正确"}, "phone_format"),
        ({"status": 429, "error": "操作过于频繁，请稍后重试"}, "rate_limited"),
        ({"status": 303, "location": "https://x/?code=abc", "code": "abc"}, "unexpected_success"),
        ({"status": 400, "error": "什么鬼"}, "other"),
    ]
    for out, want in forms:
        got = R.classify_form_result(out)
        check(f"{want}", got == want, f"{got} from {out}")

    print("[3] 'ready' requires the SMS gate, not just a good token")
    check("gate is the terminal verdict", R.NEED_SMS == "请先获取短信验证码")
    check("token accept string stable", R.TOKEN_OK == "手机号格式不正确")

    print("[4] login probe mapping (403 stale / 401 wrong password)")
    logins = [
        ((200, ""), "unexpected_success"),
        ((401, "账号或密码错误"), "auth_failed"),
        ((403, "需要先完成人机验证"), "captcha_required"),
        ((403, ""), "captcha_required"),
        ((429, "操作过于频繁"), "rate_limited"),
        ((0, "ConnectionError: boom"), "transport"),
        ((422, "Validation Error"), "http_422"),
        ((401, "需要先完成人机验证"), "captcha_required"),   # 文字优先于状态码
    ]
    for (status, detail), want in logins:
        got = R.classify_login_probe(status, detail)
        check(f"{status} {detail[:18]!r} -> {want}", got == want, got)
    check("probe user cannot exist", R.LOGIN_USER.startswith("probe-never-exists"),
          R.LOGIN_USER)

    print("ALL REHEARSE TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
