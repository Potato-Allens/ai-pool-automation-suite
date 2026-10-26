# -*- coding: utf-8 -*-
"""Live rehearsal: prove the whole client stack without spending an SMS or creating an account.

The trick
---------
``POST /register/send-code`` validates the Aliyun captcha **before** the phone
number, so probing it with a deliberately invalid number (``123``) tells us
whether a captcha token is good:

    200 {"ok": false, "message": "手机号格式不正确"}      -> token ACCEPTED, nothing was sent
    200 {"ok": false, "message": "Captcha verification rejected"} -> token bad/expired/consumed
    200 {"ok": false, "message": "Unsupported captcha platform"}   -> token field empty

Then a ``POST /register`` with a never-used valid number + a bogus SMS code and a
fresh token must stop at ``请先获取短信验证码`` -- which proves every hop except the
actual SMS delivery.

Usage
-----
    python vmax_rehearse.py                    # solve 2 tokens, full rehearsal
    python vmax_rehearse.py --token eyJ...     # reuse a token from the farm
    python vmax_rehearse.py --skip-solve --token eyJ...

Nothing here sends an SMS or creates an account. Exit 0 = READY.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()


from vmax_client import VmaxAuth, pkce_pair, VmaxError  # noqa: E402

# Login probe: a username that cannot exist, so a 401/403 pair proves the endpoint
# contract without touching any real account.
LOGIN_USER = "probe-never-exists-0000"
LOGIN_PASSWORD = "WrongPass12345"

TOKEN_OK = "手机号格式不正确"
TOKEN_BAD = "Captcha verification rejected"
TOKEN_MISSING = "Unsupported captcha platform"
NEED_SMS = "请先获取短信验证码"


def classify_probe_message(msg: str) -> str:
    """Map a /register/send-code message to a verdict (pure, unit-tested)."""
    m = str(msg or "")
    if TOKEN_MISSING in m:
        return "token_missing"
    if TOKEN_BAD in m:
        return "token_rejected"
    if m.strip() == TOKEN_OK:
        return "token_accepted"
    if "过于频繁" in m:
        return "cooldown"
    return "other"


def classify_form_result(out: dict) -> str:
    """Map a register_form() result to a verdict (pure, unit-tested)."""
    err = str(out.get("error") or "")
    if out.get("status") in (301, 302, 303) or out.get("code"):
        return "unexpected_success"
    if NEED_SMS in err:
        return "reached_sms_gate"
    if TOKEN_BAD in err:
        return "token_rejected"
    if TOKEN_MISSING in err:
        return "token_missing"
    if "手机号格式" in err:
        return "phone_format"
    if "过于频繁" in err:
        return "rate_limited"
    return "other"


def classify_login_probe(status: int, detail: str = "") -> str:
    """Map POST /api/auth/login to a verdict (pure, unit-tested).

    openapi: 200 = JWT, 401 = bad username/password, 403 = captcha required.
    A stale token must give 403 and a valid token with a wrong password must give
    401 -- that pair is the whole point of the live probe.
    """
    if status == 200:
        return "unexpected_success"
    if status == 403 or "人机验证" in str(detail or ""):
        return "captcha_required"
    if status == 401:
        return "auth_failed"
    if status == 429:
        return "rate_limited"
    if status == 0:
        return "transport"
    return f"http_{status}"


def _login_probe(cli: VmaxAuth, token: str, platform: str = "dreammaker") -> dict:
    """One POST /api/auth/login with a non-existent account (no SMS, no write bucket)."""
    t0 = time.time()
    try:
        out = cli.login_password(LOGIN_USER, LOGIN_PASSWORD, token, platform=platform)
        status = int(out.get("status") or 0)
        detail = str(out.get("detail") or out.get("message") or out.get("error") or "")[:160]
    except Exception as e:  # noqa: BLE001 - a probe must never kill the rehearsal
        status, detail = 0, f"{type(e).__name__}: {e}"
    return {"step": "login_probe", "status": status, "message": detail,
            "verdict": classify_login_probe(status, detail),
            "dt": round(time.time() - t0, 2)}


def _send_code_probe(cli: VmaxAuth, token: str, phone: str = "123") -> dict:
    t0 = time.time()
    r = cli.s.post(f"{cli.base}/register/send-code",
                   json={"phone": phone, "client_id": cli.client_conf()["client_id"],
                         "locale": "zh-CN", "captcha_verify_param": token},
                   headers={"Origin": cli.base, "Referer": f"{cli.base}/login"}, timeout=25)
    body = {}
    try:
        body = r.json()
    except ValueError:
        pass
    msg = str(body.get("message") or body.get("detail") or r.text[:120])
    verdict = classify_probe_message(msg)
    return {"step": f"probe({phone})", "status": r.status_code, "message": msg,
            "verdict": verdict, "dt": round(time.time() - t0, 2)}


def _form_probe(cli: VmaxAuth, token: str, phone: str) -> dict:
    v, ch = pkce_pair()
    t0 = time.time()
    out = cli.register_form(phone, "123456", "rh" + secrets.token_hex(5), "Aa12345678901",
                            v, ch, secrets.token_urlsafe(12), token)
    err = out.get("error") or ""
    verdict = classify_form_result(out)
    return {"step": "register_form", "status": out.get("status"), "message": err[:160],
            "verdict": verdict, "dt": round(time.time() - t0, 2)}


def solve_token(args, cli: VmaxAuth) -> tuple[str | None, dict]:
    from vmax_captcha import InpaintingSolver
    url = VmaxAuth.login_url(pkce_pair()[1], base=cli.base, redirect_uri=cli.redirect_uri)
    t0 = time.time()
    with InpaintingSolver(headless=not args.headed, proxy=args.proxy,
                          debug_dir=args.debug_dir) as sv:
        tok = sv.solve(url, max_attempts=args.attempts)
    return tok, {"step": "solve", "dt": round(time.time() - t0, 2), "chars": len(tok)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="live rehearsal without SMS / without creating accounts")
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--token", action="append", default=[], help="pre-solved token (repeatable)")
    ap.add_argument("--skip-solve", action="store_true", help="never launch the browser")
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--debug-dir", default="rehearse_debug")
    ap.add_argument("--probe-phone", default="13012345678",
                    help="valid-format number that has never received a code")
    ap.add_argument("--login-probe", action="store_true",
                    help="also prove POST /api/auth/login with a non-existent account "
                         "(one request, no captcha needed, no SMS)")
    ap.add_argument("--login-probe-only", action="store_true",
                    help="only run the login probe (no captcha, no browser) and exit")
    ap.add_argument("--out", default="rehearse.json")
    args = ap.parse_args(argv)

    cli = VmaxAuth(proxy=args.proxy, base=args.base, redirect_uri=args.redirect)
    steps: list[dict] = []
    print(f"[rehearse] base={cli.base} platform={args.platform}", flush=True)

    # R1 config
    for name, fn in (("health", lambda: cli.s.get(f"{cli.base}/health/ready", timeout=15).json()),
                     ("agreements", cli.agreements),
                     ("captcha_config", lambda: cli.captcha_config(args.platform)),
                     ("sms_config", cli.sms_config)):
        t0 = time.time()
        try:
            rec = {"step": name, "ok": True, "value": fn(), "dt": round(time.time() - t0, 2)}
        except Exception as e:  # noqa: BLE001
            rec = {"step": name, "ok": False, "value": f"{type(e).__name__}: {e}",
                   "dt": round(time.time() - t0, 2)}
        steps.append(rec)
        print(f"[rehearse] {name}: {json.dumps(rec['value'], ensure_ascii=False)[:100]}", flush=True)

    if args.login_probe_only:
        lp = _login_probe(cli, "bogus-token-not-a-real-captcha", args.platform)
        order = ("auth_failed_first" if lp["verdict"] == "auth_failed"
                 else "captcha_first" if lp["verdict"] == "captcha_required"
                 else "unexpected")
        steps.append(dict(lp, tag="login",
                          ok=lp["verdict"] in ("auth_failed", "captcha_required")))
        print(f"[rehearse] login probe: {lp['verdict']} [{lp['status']}] "
              f"{lp['message']} (order={order})", flush=True)
        ok = lp["verdict"] in ("auth_failed", "captcha_required")
        _save(args, steps, {"ts": int(time.time()), "base": cli.base,
                            "login_probe": {"verdict": lp["verdict"],
                                            "status": lp["status"], "order": order},
                            "login_ready": ok, "ready": ok,
                            "note": "login probe only: one request, no SMS, no captcha"})
        print(f"[rehearse] LOGIN_READY={ok} order={order}", flush=True)
        return 0 if ok else 1

    tokens = list(args.token)
    if not tokens and not args.skip_solve:
        print("[rehearse] solving token #1 ...", flush=True)
        try:
            tok, meta = solve_token(args, cli)
            tokens.append(tok)
            steps.append(dict(meta, ok=True))
            print(f"[rehearse] solved in {meta['dt']}s ({meta['chars']} chars)", flush=True)
        except Exception as e:  # noqa: BLE001
            steps.append({"step": "solve", "ok": False, "value": f"{type(e).__name__}: {e}"})
            print(f"[rehearse] solve FAILED: {e}", flush=True)

    if not tokens:
        steps.append({"step": "probe", "verdict": "no_token", "ok": False})
        print("[rehearse] no token available -> NOT READY", flush=True)
        _save(args, steps)
        return 1

    # R2 token validity via the invalid-phone probe (no SMS sent)
    p1 = _send_code_probe(cli, tokens[0])
    steps.append(dict(p1, ok=p1["verdict"] == "token_accepted"))
    print(f"[rehearse] probe#1: {p1['verdict']} | {p1['message']}", flush=True)

    # R3 single-use check: probe the same token again
    p2 = _send_code_probe(cli, tokens[0])
    steps.append(dict(p2, ok=True))
    print(f"[rehearse] probe#2 (same token): {p2['verdict']} | {p2['message']}", flush=True)

    # R4 form must reach the SMS gate with a fresh token
    form_token = tokens[1] if len(tokens) > 1 else (None if p2["verdict"] != "token_accepted" else tokens[0])
    if form_token is None and not args.skip_solve:
        print("[rehearse] solving token #2 ...", flush=True)
        try:
            tok, meta = solve_token(args, cli)
            form_token = tok
            steps.append(dict(meta, ok=True))
        except Exception as e:  # noqa: BLE001
            steps.append({"step": "solve2", "ok": False, "value": f"{type(e).__name__}: {e}"})

    if form_token:
        f = _form_probe(cli, form_token, args.probe_phone)
        steps.append(dict(f, ok=f["verdict"] == "reached_sms_gate"))
        print(f"[rehearse] form: {f['verdict']} | {f['message']}", flush=True)
    else:
        steps.append({"step": "register_form", "verdict": "no_fresh_token", "ok": False})

    # R5 (opt-in) login probe: one request, a non-existent account, a bogus token.
    # Live 2026-10-26: this answers 401 (credentials are checked before the captcha),
    # which is also why a 401 never consumes its token.  403 here would mean the
    # order flipped to captcha-first; both are recorded, neither is a failure.
    login_ok = True
    login_rec: dict = {}
    if args.login_probe:
        lp = _login_probe(cli, "bogus-token-not-a-real-captcha", args.platform)
        order = ("auth_failed_first" if lp["verdict"] == "auth_failed"
                 else "captcha_first" if lp["verdict"] == "captcha_required"
                 else "unexpected")
        steps.append(dict(lp, tag="login", ok=lp["verdict"] in ("auth_failed",
                                                               "captcha_required")))
        print(f"[rehearse] login probe: {lp['verdict']} [{lp['status']}] "
              f"{lp['message']} (order={order})", flush=True)
        login_rec = {"verdict": lp["verdict"], "status": lp["status"], "order": order}
        login_ok = lp["verdict"] in ("auth_failed", "captcha_required")

    solve_dt = [s["dt"] for s in steps if s.get("step") == "solve" and s.get("ok")]
    verdicts = {s.get("verdict") for s in steps if s.get("verdict")}
    ready = ("token_accepted" in verdicts and "reached_sms_gate" in verdicts
             and login_ok
             and all(s.get("ok", True) for s in steps if s.get("step") in
                     ("health", "agreements", "captcha_config", "sms_config")))
    summary = {
        "ts": int(time.time()), "base": cli.base, "ready": ready,
        "single_use": p2["verdict"] == "token_rejected",
        "solve_seconds": solve_dt,
        "token_ttl_hint": "single-use confirmed by probe#2" if p2["verdict"] == "token_rejected"
                          else "token survived a second probe (not consumed by a failed phone check)",
        "login_probe": login_rec,
        "login_ready": (login_ok if args.login_probe else None),
        "note": "no SMS was sent, no account was created",
    }
    _save(args, steps, summary)
    print(f"[rehearse] READY={ready} single_use={summary['single_use']} "
          f"solve={solve_dt}", flush=True)
    return 0 if ready else 1


def _save(args, steps, summary=None):
    out = {"ts": int(time.time()), "platform": args.platform, "steps": steps}
    if summary:
        out["summary"] = summary
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[rehearse] wrote {os.path.abspath(args.out)}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
