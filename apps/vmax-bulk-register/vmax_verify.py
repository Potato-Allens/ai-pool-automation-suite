# -*- coding: utf-8 -*-
"""One-shot full-chain verifier with per-step raw evidence.

    # with a real phone + a human-solved captcha
    python vmax_verify.py --phone 13800000000 --captcha assist --sms prompt

    # already have a code (e.g. from another device): skip captcha + send-code
    python vmax_verify.py --phone 13800000000 --skip-send-code --sms-code 123456

Every step prints what the server actually returned, and the final block shows
the OAuth code, the JWT claims and the resulting token. This is the script to
run when you finally have a real +86 number: it turns the last inferred branch
(303 Location ?code= -> /api/auth/oauth/token) into measured fact.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import secrets
import string
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vmax_client import CLIENTS, VmaxAuth, VmaxError, pkce_pair  # noqa: E402
from vmax_register import (  # noqa: E402
    classify_register_error, normalize_phone, obtain_captcha,
)
from vmax_sms import build_sms  # noqa: E402


def gen_password(n: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n - 3))
        pw = secrets.choice(string.ascii_uppercase) + secrets.choice(string.ascii_lowercase) + \
            secrets.choice(string.digits) + pw
        pw = "".join(random.sample(pw, len(pw)))
        if (any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def _soft(fn):
    """Run an evidence-only probe: never raises, always returns something printable."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a paid run must not die on a side probe
        return {"error": ("%s: %s" % (type(e).__name__, e))[:200]}


def jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="vobilemax full-chain verifier")
    ap.add_argument("--phone", required=True)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=list(CLIENTS))
    ap.add_argument("--base", default=None, help="auth base URL (env VMAX_BASE)")
    ap.add_argument("--redirect", default=None, help="redirect_uri override")
    ap.add_argument("--captcha", default="assist", choices=["auto", "manual", "assist", "param", "queue"])
    ap.add_argument("--captcha-param", default="")
    ap.add_argument("--captcha-attempts", type=int, default=6)
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=0)
    ap.add_argument("--assist-dir", default="assist")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--sms", default="prompt")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-code", default=None, help="use this code instead of a provider")
    ap.add_argument("--skip-send-code", action="store_true",
                    help="do not solve captcha / call send-code (use --sms-code)")
    ap.add_argument("--sms-timeout", type=int, default=180)
    ap.add_argument("--sms-resends", type=int, default=0,
                    help="on 验证码不正确/次数过多, wait the cooldown and re-send (max N)")
    ap.add_argument("--register-captcha-param", default="",
                    help="captcha param for the /register form (default: empty, matches observed flow)")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--debug-dir", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    phone = normalize_phone(args.phone)
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        print(f"[verify] phone format invalid: {phone!r}", file=sys.stderr)
        return 1

    cli = VmaxAuth(proxy=args.proxy, base=args.base, redirect_uri=args.redirect)
    conf = cli.client_conf(args.platform)
    state = secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()
    login_url = VmaxAuth.login_url(challenge, state=state, client=args.platform,
                                   base=cli.base, redirect_uri=cli.redirect_uri)
    evidence: dict = {"phone": phone, "base": cli.base,
                      "redirect_uri": conf["redirect_uri"], "steps": []}

    def step(name: str, data) -> None:
        evidence["steps"].append({"step": name, "ts": int(time.time()), "data": data})
        print(f"[verify] {name}: {json.dumps(data, ensure_ascii=False)[:400]}", flush=True)

    # 1) device cookie (auth_fingerprint only counts after auth_device exists)
    r = cli.s.get(login_url, timeout=cli.timeout)
    fp = secrets.token_hex(16)
    # hostname only: "127.0.0.1:8080" is not a cookie domain, so a mock base used
    # to drop the fingerprint and signup-check answered requires_captcha=true.
    host = urlparse(cli.base).hostname or ""
    cli.s.cookies.set("auth_fingerprint", fp, domain=host, path="/")
    step("device", {"status": r.status_code, "auth_device": "auth_device" in cli.s.cookies.get_dict(),
                    "fingerprint": fp[:8] + "..."})
    step("signup-check", cli.s.get(f"{cli.base}/api/auth/signup-check", timeout=cli.timeout).json())
    step("agreements", [{"id": a["id"], "type": a["type"]} for a in cli.agreements()])

    # 2) captcha + send-code
    code = args.sms_code
    last_send = time.time()
    if not args.skip_send_code:
        param = obtain_captcha(args, login_url)
        step("captcha", {"param_len": len(param)})
        t_send = time.time()   # request start, not response time
        sent = cli.send_register_code(phone, param, client=args.platform)
        step("send-code", sent)
        if not sent.get("ok"):
            evidence["status"] = "stopped-at-send-code"
            return finish(evidence, args, 2)
        last_send = t_send
    else:
        step("send-code", {"skipped": True})

    if not code:
        code = build_sms(args.sms, args.sms_dir).wait(phone, timeout=args.sms_timeout,
                                                      since=last_send)
        step("sms", {"code": code[:2] + "****"})
    else:
        step("sms", {"code": "provided", "len": len(code)})

    # 3) register (native form POST; success = 303 to redirect_uri?code=...)
    username = args.username or ("u" + secrets.token_hex(5))
    password = args.password or gen_password()
    resends = max(0, int(getattr(args, "sms_resends", 0) or 0))
    resend_no = 0
    while True:
        out = cli.register_form(phone, code, username, password, verifier, challenge, state,
                                args.register_captcha_param, invitation_code="",
                                client=args.platform)
        step("register" + (f"#{resend_no + 1}" if resend_no else ""), out)
        if out.get("code"):
            break
        kind = classify_register_error(out.get("error"))
        evidence.setdefault("register_errors", []).append({"error": out.get("error"), "kind": kind})
        if kind in ("code_wrong", "code_locked", "code_expired") and resend_no < resends:
            resend_no += 1
            wait_s = float(os.environ.get("VMAX_RESEND_WAIT", "62"))
            left = wait_s - (time.time() - last_send)
            if left > 0:
                step("resend-wait", {"seconds": round(left, 1)})
                time.sleep(left)
            param = obtain_captcha(args, login_url)
            t_send = time.time()
            sent = cli.send_register_code(phone, param, client=args.platform)
            step(f"resend#{resend_no}", sent)
            last_send = t_send
            if not sent.get("ok"):
                evidence["status"] = "stopped-at-resend"
                return finish(evidence, args, 2)
            code = build_sms(args.sms, args.sms_dir).wait(phone, timeout=args.sms_timeout,
                                                          since=last_send)
            step("sms-after-resend", {"code": code[:2] + "****"})
            continue
        evidence["status"] = "stopped-at-register"
        return finish(evidence, args, 2)
    evidence["code"] = out["code"]
    evidence["state_echoed"] = f"state={state}" in (out.get("location") or "")

    # 3.5) zero-SMS corroboration of two things a 303 alone does not prove:
    #      (a) the cookie session really is authenticated, and
    #      (b) the accepted_agreement_ids posted with the form were persisted
    #          for the *active* versions (otherwise every account starts with an
    #          unsigned agreement and the product keeps gating it).
    #      Read-only unless the server says "not accepted"; then we repair once.
    evidence["session"] = _soft(cli.session)
    status = _soft(cli.agreement_status)
    evidence["agreements_initial"] = status
    healed = None
    if isinstance(status, list) and any(a.get("accepted") is False for a in status):
        ids = _soft(cli.agreement_ids)
        csrf = (evidence.get("session") or {}).get("csrf_token", "") \
            if isinstance(evidence.get("session"), dict) else ""
        healed = {"accept": _soft(lambda: cli.accept_agreements(
                      ids if isinstance(ids, list) else [], "vmax_verify",
                      csrf_token=str(csrf or "")))}
        healed["before"] = status
        healed["after"] = _soft(cli.agreement_status)
        status = healed["after"]
        evidence["agreements_healed"] = healed
    evidence["agreements"] = status
    step("session", evidence["session"])
    step("agreements-accepted", status)
    if healed:
        step("agreements-healed", healed)

    # 4) code -> JWT
    tok = cli.exchange_code(out["code"], verifier, client=args.platform)
    access = tok.get("access_token", "")
    evidence["token"] = {"token_type": tok.get("token_type"), "expires_in": tok.get("expires_in"),
                         "claims": jwt_claims(access)}
    step("oauth-token", {"token_type": tok.get("token_type"),
                         "expires_in": tok.get("expires_in"),
                         "claims": evidence["token"]["claims"]})
    evidence["status"] = "ok"
    return finish(evidence, args, 0)


def finish(evidence: dict, args, rc: int) -> int:
    print("\n[verify] summary:")
    print(json.dumps(evidence, ensure_ascii=False, indent=1))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VmaxError, RuntimeError) as e:
        print(f"[verify] FAILED: {e}", file=sys.stderr)
        raise SystemExit(1)