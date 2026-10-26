# -*- coding: utf-8 -*-
"""Recover an account whose number is already registered (password reset).

Why this exists
---------------
``/register/send-code`` refuses a number that already has an account, and the
runners treat that as terminal (``phone_taken`` / exit 7).  For a **recycled
SIM** that is the common case -- and skipping the card throws it away.  The
service has the standard recovery flow for exactly this situation (openapi):

    1. POST /api/auth/reset-password/send-code
         {phone, captcha_platform, captcha_verify_param}
         -> {"sent": true, ...}      404 = not registered, 429 = daily cap
    2. POST /api/auth/reset-password
         {phone, sms_code, new_password}
         -> AuthResponse{access_token, user}      **no captcha at all**

So one captcha token + one SMS turns a dead card into an account we can log into
with a password we now know.  It is also the only way back from a crash that
lost a result row *after* ``/register`` succeeded: the account exists, the
password does not.

Cost per number: 1 captcha token + 1 SMS -- exactly a registration.  Unlike
``/register``, the reset send-code does *not* accept the device path: with a
trusted ``auth_device`` + ``auth_fingerprint`` and an empty
``captcha_verify_param`` it still answers ``400 Incomplete captcha parameters``,
exactly like a cookieless request (R142, measured on an unregistered number,
0 SMS).  So there is no ``--form-captcha device`` here on purpose.  Because
step 2 needs no captcha, a token failure can never strand a spent SMS.

Rows look like registration rows (``status=ok`` + ``password`` + ``uid``) plus
``via="reset"``, so ``vmax_login`` / ``vmax_report`` need no special case; the
report just counts them separately as ``reused``.

    python vmax_reset.py --phone 13800000000 --sms "http://127.0.0.1:8787/code?phone={phone}&since={since}"
    python vmax_reset.py --input results.jsonl --out results.jsonl --captcha queue

Exit codes: 0 all ok, 1 some failed, 2 bad args / empty input, 3 daily/rate limit
(stop, do not poll), 4 nothing to do.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()


from vmax_client import VmaxAuth, VmaxError, pkce_pair  # noqa: E402
from vmax_register import (PHONE_RE, classify_register_error, gen_password,  # noqa: E402
                           normalize_phone, obtain_captcha)
from vmax_report import is_taken, load_rows  # noqa: E402
from vmax_sms import build_sms  # noqa: E402

# Reset has two answers the register path never produces.  Check them first and
# then fall back to the shared vocabulary (code_wrong / code_expired /
# captcha_rejected / rate_limited ...), so both runners bucket errors the same way.
RESET_EXTRA_RULES = (
    ("not_registered", "未注册"),      # 404: the number has no account after all
    ("daily_limit", "次数超限"),        # 429: per-phone / per-day SMS cap
    ("daily_limit", "每日"),
    # 400 on step 2: the openapi words it as "验证码或新密码无效" -- one bucket for
    # both, because a retry needs a fresh code either way.
    ("code_wrong", "验证码或新密码无效"),
)


def classify_reset_error(msg) -> str:
    text = str(msg or "").strip()
    for kind, needle in RESET_EXTRA_RULES:
        if needle in text:
            return kind
    return classify_register_error(text)


RESET_SEND_CODE = "/api/auth/reset-password/send-code"
RESET_PASSWORD = "/api/auth/reset-password"
RESET_REQUEST = "ResetPasswordRequest"


def probe_schema(cli: VmaxAuth, timeout: float = 30) -> dict:
    """Does the published schema still describe the flow we depend on?

    Pure GET: no captcha token, no SMS, no account.  A site change that drops
    the reset routes -- or starts demanding a captcha in step 2 -- shows up here
    long before a run spends anything.
    """
    doc = cli.s.get(f"{cli.base}/openapi.json", timeout=timeout).json()
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    schemas = ((doc.get("components") or {}).get("schemas") or {})
    props = (schemas.get(RESET_REQUEST) or {}).get("properties") or {}
    fields = sorted(props)
    return {
        "has_send_code": RESET_SEND_CODE in paths,
        "has_reset": RESET_PASSWORD in paths,
        "has_login": "/api/auth/login" in paths,
        "request_fields": fields,
        # Step 2 must stay captcha-free: that is what stops a spent token from
        # stranding a delivered SMS (R105).
        "captcha_free": "captcha_verify_param" not in fields,
        "paths": len(paths),
    }


def probe_live(cli: VmaxAuth, phone: str = "13900139000") -> dict:
    """One empty-captcha POST to the reset send-code endpoint.

    The server checks captcha *presence* first (R105: 400 Incomplete captcha
    parameters), so this request cannot reach the phone lookup and cannot send
    an SMS.  It proves the route is wired and the gate still answers as the
    runner expects.
    """
    out = cli.send_reset_code(phone, "")
    msg = str(out.get("message") or "")
    return {"status": out.get("status"), "message": msg[:80],
            "ok": out.get("ok") is False and "Incomplete captcha" in msg}


def reset_one(args, phone: str, cli: VmaxAuth | None = None) -> dict:
    """Reset the password of an existing account and return a result row.

    Never raises for a *server* answer (404/429/400 all come back as
    ``status=error`` + ``reset_error_kind``), so a caller can keep going.
    """
    phone = normalize_phone(phone)
    if not PHONE_RE.match(phone):
        raise VmaxError(f"phone format invalid: {phone!r} (need 11-digit mainland number)")

    password = gen_password()
    platform = getattr(args, "platform", "dreammaker")
    row: dict = {"phone": phone, "username": None, "password": password,
                 "via": "reset", "platform": platform, "ts": int(time.time())}

    cli = cli or VmaxAuth(proxy=getattr(args, "proxy", None),
                          base=getattr(args, "base", None),
                          redirect_uri=getattr(args, "redirect", None))
    row["base"] = cli.base

    # The captcha widget only exists on the login page, so the solver needs a real
    # /login URL.  Reset itself needs no PKCE / no redirect round trip.
    _verifier, challenge = pkce_pair()
    login_url = VmaxAuth.login_url(challenge, state=secrets.token_urlsafe(16),
                                   client=platform, base=cli.base,
                                   redirect_uri=cli.redirect_uri)

    t0 = time.time()
    param = obtain_captcha(args, login_url)
    row["captcha_seconds"] = round(time.time() - t0, 1)
    print(f"[{phone}] reset captcha ok ({row['captcha_seconds']}s)", flush=True)

    # No pacer here on purpose: /api/auth/* is outside the /register write bucket
    # (see PROTOCOL S7.2), so a register ban says nothing about this endpoint.
    t_send = time.time()    # request start: the SMS may land before the reply
    r = cli.send_reset_code(phone, param, client=platform)
    row["send_code"] = r
    print(f"[{phone}] reset send-code {r}", flush=True)
    if not r.get("ok"):
        msg = str(r.get("message") or r.get("detail") or "")
        row.update(status="error", reset_error_kind=classify_reset_error(msg),
                   error=f"reset send-code rejected: {msg}",
                   register_http_status=r.get("status"))
        return row

    sms = build_sms(getattr(args, "sms", "none"), getattr(args, "sms_dir", "sms_inbox"))
    code = sms.wait(phone, timeout=getattr(args, "sms_timeout", 300), since=t_send)
    row["sms_seconds"] = round(time.time() - t_send, 1)
    print(f"[{phone}] reset sms code {code}", flush=True)

    try:
        tok = cli.reset_password(phone, code, password)
    except VmaxError as e:
        row.update(status="error", reset_error_kind=classify_reset_error(str(e)),
                   error=f"{type(e).__name__}: {e}", register_http_status=e.status)
        print(f"[{phone}] reset failed: {e}", flush=True)
        return row

    user = tok.get("user") if isinstance(tok.get("user"), dict) else {}
    row.update(token=tok, uid=user.get("uid"), status="ok")
    print(f"[{phone}] reset ok uid={row.get('uid')}", flush=True)
    return row


def load_done(path: str) -> set[str]:
    """Phones that already have a status=ok row (a previous reset / register)."""
    done: set[str] = set()
    if not path or not os.path.exists(path):
        return done
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("status") == "ok" and r.get("phone"):
                done.add(normalize_phone(str(r["phone"])))
    return done


def load_limited(path: str, day: str | None = None) -> set[str]:
    """Phones whose latest row *today* is ``daily_limit``.

    The 429 for a capped number arrives after the captcha gate, so a second run
    on the same day would spend one token per retry to learn the same thing.
    Skipping them is free; ``--retry-limited`` forces the attempt anyway (site
    quota reset, egress changed, or the cap turned out to be per-IP).
    """
    out: set[str] = set()
    if not path or not os.path.exists(path):
        return out
    day = day or time.strftime("%Y-%m-%d")
    latest: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("phone"):
                latest[normalize_phone(str(r["phone"]))] = r   # append order: last wins
    for phone, r in latest.items():
        if r.get("status") == "ok" or r.get("reset_error_kind") != "daily_limit":
            continue
        try:
            when = time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts"))))
        except (TypeError, ValueError):
            continue
        if when == day:
            out.add(phone)
    return out


def pick_targets(args) -> list[str]:
    if args.phone:
        return [normalize_phone(args.phone)]
    rows = load_rows(args.input)
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not args.all and not is_taken(r):
            continue
        phone = normalize_phone(str(r.get("phone") or ""))
        if phone and phone not in seen:
            seen.add(phone)
            out.append(phone)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="recover an already-registered number via password reset")
    ap.add_argument("--phone", default=None, help="one number")
    ap.add_argument("--input", default=None,
                    help="results.jsonl -- every phone the server said is already registered")
    ap.add_argument("--out", default=None, help="append rows here (default: --input)")
    ap.add_argument("--all", action="store_true", help="reset every row, not only taken ones")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-limited", action="store_true",
                    help="also try numbers whose latest row today is daily_limit "
                         "(each such attempt costs one captcha token)")
    ap.add_argument("--captcha", default="queue",
                    choices=["auto", "manual", "assist", "param", "queue"])
    ap.add_argument("--captcha-param", default="")
    ap.add_argument("--captcha-attempts", type=int, default=6)
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=600)
    ap.add_argument("--sms", default="http://127.0.0.1:8787/code?phone={phone}")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-timeout", type=int, default=300)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--debug-dir", default="captcha_debug")
    ap.add_argument("--assist-dir", default="assist")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)

    if not args.phone and not args.input:
        ap.error("--phone or --input is required")
    out = args.out or args.input
    if not out:
        ap.error("--out is required with --phone when no input file is given")

    targets = pick_targets(args)
    # Both facts may live in either file: --input is often a results.jsonl from
    # an earlier runner (the cap row is there) while --out is a fresh file, and
    # with no --out they are the same path.
    done: set[str] = set()
    capped: set[str] = set()
    for path in (args.input, out):
        if not path:
            continue
        done |= load_done(path)
        if not args.retry_limited:
            capped |= load_limited(path)
    n_capped = len([p for p in targets if p not in done and p in capped])
    targets = [p for p in targets if p not in done and p not in capped]
    if n_capped:
        print(f"[reset] {n_capped} phone(s) already hit today's cap -> skipped "
              f"(0 token spent; --retry-limited to force, or resume tomorrow)",
              flush=True)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("[reset] nothing to do (no taken row without an ok row)", flush=True)
        return 4

    print(f"[reset] {len(targets)} phone(s) to recover -> {out}", flush=True)
    ok = fail = 0
    kinds: dict[str, int] = {}
    for i, phone in enumerate(targets, 1):
        row = reset_one(args, phone)
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if row.get("status") == "ok":
            ok += 1
            print(f"[reset] {i}/{len(targets)} {phone} ok uid={row.get('uid')}", flush=True)
            continue
        fail += 1
        kind = row.get("reset_error_kind") or "error"
        kinds[kind] = kinds.get(kind, 0) + 1
        print(f"[reset] {i}/{len(targets)} {phone} {kind}: "
              f"{str(row.get('error'))[:80]}", flush=True)
        if kind == "daily_limit":
            print("[reset] daily/rate limit -> stop (a new code will not arrive today); "
                  "resume tomorrow with the same --out", flush=True)
            print(f"[reset] ok={ok} fail={fail} kinds={kinds}", flush=True)
            return 3

    print(f"[reset] done: ok={ok} fail={fail} kinds={kinds}", flush=True)
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
