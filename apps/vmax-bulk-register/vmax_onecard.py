# -*- coding: utf-8 -*-
r"""One card, one SMS, one protocol answer.

The batch toolkit is done; what is left in docs/PROTOCOL.md are questions that
need a real message.  This tool turns each of them into one command with a
written verdict, so the first card bought does not get spent on ad-hoc poking:

    # is the SMS code bound to the session that asked for it?
    python vmax_onecard.py xdevice --phone 138... --sms "http:..." --out runs\onecard

    # how long is the code actually alive? (costs one SMS per probe)
    python vmax_onecard.py ttl --phone 138... --sms "http:..." --low 60 --high 600 --rounds 3

    # a code IS pending, but the form carries no sms_code at all
    python vmax_onecard.py nosms --phone 138... --sms "http:..." --out runs\onecard

``xdevice`` is the interesting one: session A sends the code, session B (fresh
cookie jar, its own PKCE/state and its own captcha token) submits it.  If B gets
a 303 with ``code=`` then redemption follows the *phone*, not the session --
which is exactly what "can a code be received on one device and typed on
another" means.  If B is refused with "code incorrect", the code is bound to
something in the sending session and the conservative advice in PROTOCOL §7
stands.

``nosms`` closes the last open cell of PROTOCOL §5.2.2: openapi does not list
``sms_code`` as required, and a phone with *no* pending code is refused with
"请先获取短信验证码" -- but nobody had measured "code pending, field absent".
It asks for the code once (one SMS), waits for it, then submits ``/register``
with the field **dropped**; only if that is refused for the value does it try
``sms_code=""`` (one more token, no SMS), which separates "field ignored" from
"field read as the empty string".  A 303 on either variant means the handler
trusts the pending record and the code value is decorative -- a registration
bypass that must be reported, not just filed as a protocol note.

Token cost: ``--form-captcha device`` (the default) walks the site's own
signup-check path, so the *form* costs nothing while the device is trusted --
``xdevice`` is 1 token, ``nosms`` is 1 (+1 for the second variant), ``ttl`` is
1 per probe instead of 2.  A refused trust falls back to a paid token and says
so in the verdict (``form.form_captcha = device_fallback / device_retry``).

Exit codes: 0 a verdict was recorded, 1 inconclusive (expired / unexpected
answer / no code), 2 bad arguments, 3 the write bucket answered 429,
4 the contract gate refused (drift or unreachable -- 0 token, 0 SMS),
9 the SMS channel is down (0 token, 0 SMS spent).  ``--dry-run`` prints the
plan and spends nothing.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vmax_client import VmaxAuth, VmaxError, pkce_pair  # noqa: E402
from vmax_register import (classify_register_error, device_form_param,  # noqa: E402
                          gen_password, gen_username, normalize_phone,
                          obtain_captcha)
from vmax_sms import SmsTimeout, build_sms, channel_check  # noqa: E402

CODE_ACCEPTED = "cross_session_accepted"
CODE_BOUND = "cross_session_rejected"
NOSMS_ACCEPT = {"omit": "accepted_without_code", "empty": "accepted_with_empty_code"}


def mask(phone: str) -> str:
    phone = str(phone or "")
    return phone[:3] + "****" + phone[-4:] if len(phone) >= 8 else phone


def new_auth(a) -> VmaxAuth:
    """A session with its own cookie jar (this is the whole point of xdevice)."""
    return VmaxAuth(proxy=a.proxy, base=a.base, redirect_uri=a.redirect)


def captcha_for(a, auth: VmaxAuth, challenge: str, state: str) -> str:
    url = VmaxAuth.login_url(challenge, state, base=auth.base,
                             redirect_uri=auth.redirect_uri)
    return obtain_captcha(a, url)


def form_submit(a, auth, phone: str, code: str, username: str, password: str,
                verifier: str, challenge: str, state: str, **kw):
    """POST /register with the configured form-captcha policy.

    Every experiment here submits the form at least once, and until R141 each
    submit bought its own token.  ``--form-captcha device`` (the default) walks
    the site's own path instead: GET /login -> auth_fingerprint ->
    /api/auth/signup-check -> empty ``captcha_verify_param`` while the device is
    trusted.  That is 0 extra tokens, and the fallbacks are explicit:

      * signup-check refuses / the call fails -> device_fallback (one token)
      * /register still answers "Unsupported captcha platform" after a trusted
        verdict -> device_retry (one token, submit again)

    Returns (answer, meta); ``meta`` goes into the verdict JSON (no secrets).
    """
    policy = getattr(a, "form_captcha", "auto") or "auto"
    url = VmaxAuth.login_url(challenge, state, base=auth.base,
                             redirect_uri=auth.redirect_uri)
    meta: dict = {"policy": policy}
    if policy == "device":
        param = device_form_param(a, auth, url, phone, meta, tag="onecard")
        meta.setdefault("form_captcha", "device")
    else:
        param = obtain_captcha(a, url)
        meta["form_captcha"] = policy
    out = auth.register_form(phone, code, username, password, verifier, challenge,
                             state, param, **kw)

    kind = classify_register_error(out.get("error") or "")
    if out.get("code") or policy == "reuse" or kind != "captcha_rejected":
        return out, meta
    print("[onecard] the form was refused for its captcha; solving one more token",
          flush=True)
    param = obtain_captcha(a, url)
    out = auth.register_form(phone, code, username, password, verifier, challenge,
                             state, param, **kw)
    meta["captcha_retry"] = True
    meta["form_captcha"] = "device_retry" if policy == "device" else "auto_retry"
    return out, meta


def wait_code(a, phone: str, since: float) -> str:
    return build_sms(a.sms, a.sms_dir).wait(phone, timeout=a.sms_timeout, since=since)


def record_result(a, row: dict) -> None:
    """Append the account (with its password) to a results.jsonl, like the runners."""
    if not a.results:
        return
    d = os.path.dirname(os.path.abspath(a.results))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(a.results, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def verdict_kind(out: dict) -> str:
    """Map a /register answer to a stable bucket."""
    if out.get("code"):
        return CODE_ACCEPTED
    kind = classify_register_error(out.get("error") or "")
    if kind in ("code_wrong", "code_locked"):
        return CODE_BOUND
    if kind == "code_expired":
        return "inconclusive_expired"
    if kind == "phone_taken":
        return "phone_taken"
    if kind == "rate_limited":
        return "rate_limited"
    if out.get("status") == 429:
        return "rate_limited"
    return "error_" + (kind or "unknown")


def nosms_kind(out: dict, variant: str) -> str:
    """Bucket a /register answer for a request that carries no usable code value."""
    if out.get("code"):
        return NOSMS_ACCEPT[variant]
    err = out.get("error") or ""
    kind = classify_register_error(err)
    if kind == "code_wrong":
        return "code_value_required"      # the handler compares it: "" is a wrong code
    if kind == "code_expired":
        return "code_expired"
    if "请先获取短信验证码" in err:
        return "no_pending_code_seen"     # the pending record is not visible there
    if kind == "phone_taken":
        return "phone_taken"
    if kind == "rate_limited" or out.get("status") == 429:
        return "rate_limited"
    if out.get("status") == 422:
        return "schema_rejected"
    return "error_" + (kind or "unknown")

def finish_account(a, auth, out: dict, verifier: str, username: str,
                   password: str, mode: str) -> dict:
    """Exchange the 303 code, then append the account to results.jsonl."""
    uid = None
    tok: dict = {}
    if a.exchange:
        try:
            tok = auth.exchange_code(out["code"], verifier)
            uid = (tok.get("user") or {}).get("uid")
        except VmaxError as e:
            tok = {"error": str(e)[:200]}
    record_result(a, {"phone": a.phone, "username": username, "password": password,
                      "status": "ok", "uid": uid, "via": mode, "ts": int(time.time())})
    return {"uid": uid, "token_type": tok.get("token_type"),
            "has_access_token": bool(tok.get("access_token")),
            "exchange_error": tok.get("error")}


def experiment_xdevice(a) -> dict:
    """Session A asks for the code, session B submits it."""
    A, B = new_auth(a), new_auth(a)
    vA, cA = pkce_pair()
    sA = secrets.token_urlsafe(16)
    pA = captcha_for(a, A, cA, sA)
    t0 = time.time()
    A.send_register_code(a.phone, pA)
    print("[onecard] code requested in session A (%s)" % mask(a.phone), flush=True)
    code = wait_code(a, a.phone, t0)
    print("[onecard] code received in A, submitting it from session B", flush=True)

    vB, cB = pkce_pair()
    sB = secrets.token_urlsafe(16)
    username = a.username or gen_username()
    password = a.password or gen_password()
    out, form = form_submit(a, B, a.phone, code, username, password, vB, cB, sB,
                            invitation_code=a.invite, client=a.platform)
    answer = verdict_kind(out)
    rec = {"mode": "xdevice", "phone": a.phone, "username": username,
           "answer": answer, "sent_in": "A", "submitted_in": "B",
           "register": {"status": out.get("status"),
                        "redirect": bool(out.get("location")),
                        "code_present": bool(out.get("code")),
                        "error": (out.get("error") or "")[:200]},
           "form": form, "ts": int(time.time())}
    if answer == CODE_ACCEPTED:
        rec.update(finish_account(a, B, out, vB, username, password, "onecard_xdevice"))
    return rec


def experiment_ttl(a) -> dict:
    """How long is the code alive?  Each probe costs one SMS.

    A *successful* probe also creates the account, so the number cannot be
    reused to bisect upwards: the run stops at the first accepted code and
    reports `ttl_min_s` as a lower bound.  Expired probes are repeatable, which
    is what makes a downward bisect (``--high`` too big) work.
    """
    lo, hi = float(a.low), float(a.high)
    if hi < lo:
        lo, hi = hi, lo
    rounds = []
    last_send = 0.0
    answer = "inconclusive"
    for _ in range(max(1, int(a.rounds))):
        wait = (lo + hi) / 2.0 if hi > lo else lo
        gap = last_send + float(a.cooldown) - time.time()
        if gap > 0:
            print("[onecard] waiting %.0fs for the resend cooldown" % gap, flush=True)
            time.sleep(gap)
        A = new_auth(a)
        v1, c1 = pkce_pair()
        s1 = secrets.token_urlsafe(16)
        p1 = captcha_for(a, A, c1, s1)
        t0 = time.time()
        A.send_register_code(a.phone, p1)
        last_send = time.time()
        code = wait_code(a, a.phone, t0)
        if wait > 0:
            time.sleep(wait)
        v2, c2 = pkce_pair()
        s2 = secrets.token_urlsafe(16)
        username = a.username or gen_username()
        password = a.password or gen_password()
        out, form = form_submit(a, A, a.phone, code, username, password, v2, c2, s2,
                                invitation_code=a.invite, client=a.platform)
        kind = verdict_kind(out)
        rounds.append({"wait_s": round(wait, 2), "status": out.get("status"),
                       "answer": kind, "form": form,
                       "error": (out.get("error") or "")[:120]})
        print("[onecard] ttl probe wait=%.2fs -> %s" % (wait, kind), flush=True)
        if kind == CODE_ACCEPTED:
            lo = max(lo, wait)
            account = finish_account(a, A, out, v2, username, password, "onecard_ttl")
            rounds[-1]["account"] = account
            break                      # the number now has an account: no upward probe
        if kind == "inconclusive_expired":
            hi = min(hi, wait)
        else:
            break
        if hi - lo <= float(a.precision):
            break
    if rounds and hi > lo:
        answer = "ttl_between"
    return {"mode": "ttl", "phone": a.phone, "answer": answer,
            "ttl_min_s": round(lo, 2), "ttl_max_s": round(hi, 2),
            "precision_s": a.precision, "rounds": rounds, "ts": int(time.time())}

def experiment_nosms(a) -> dict:
    """Code pending + field absent: does /register still check the value?

    One token + one SMS buys the precondition ("a code exists for this phone"),
    then the form is submitted without ``sms_code``.  Only a refusal *for the
    value* triggers the second variant (``sms_code=""``, one more token).
    """
    A = new_auth(a)
    v1, c1 = pkce_pair()
    s1 = secrets.token_urlsafe(16)
    p1 = captcha_for(a, A, c1, s1)
    t0 = time.time()
    A.send_register_code(a.phone, p1)
    print("[onecard] code requested (%s)" % mask(a.phone), flush=True)
    code = wait_code(a, a.phone, t0)
    print("[onecard] code received; submitting /register WITHOUT sms_code", flush=True)

    username = a.username or gen_username()
    password = a.password or gen_password()
    attempts = []

    v2, c2 = pkce_pair()
    s2 = secrets.token_urlsafe(16)
    out, form = form_submit(a, A, a.phone, "", username, password, v2, c2, s2,
                            invitation_code=a.invite, client=a.platform,
                            omit_sms_code=True)
    answer = nosms_kind(out, "omit")
    attempts.append({"variant": "omit", "status": out.get("status"), "answer": answer,
                     "form": form, "error": (out.get("error") or "")[:160]})
    print("[onecard] omit  -> %s" % answer, flush=True)
    account = None
    if out.get("code"):
        account = finish_account(a, A, out, v2, username, password, "onecard_nosms")
    elif answer == "code_value_required":
        v3, c3 = pkce_pair()
        s3 = secrets.token_urlsafe(16)
        out2, form2 = form_submit(a, A, a.phone, "", username, password, v3, c3, s3,
                                  invitation_code=a.invite, client=a.platform)
        answer = nosms_kind(out2, "empty")
        attempts.append({"variant": "empty", "status": out2.get("status"),
                         "answer": answer, "form": form2,
                         "error": (out2.get("error") or "")[:160]})
        print("[onecard] empty -> %s" % answer, flush=True)
        if out2.get("code"):
            account = finish_account(a, A, out2, v3, username, password, "onecard_nosms")

    rec = {"mode": "nosms", "phone": a.phone, "username": username,
           "answer": answer, "attempts": attempts,
           "code_pending": bool(code), "ts": int(time.time())}
    if account:
        rec["account"] = account
    return rec


CONTRACT_DRIFT = "contract_drift"
CONTRACT_UNAVAILABLE = "contract_unavailable"


def contract_gate(a) -> tuple[int, str]:
    """Check the server contract before the first single-use token is spent.

    Returns (exit, one-line detail).  exit 0 means "go", 1 drift, 2 unavailable.
    The suite monkeypatches this, so tests never need a live contract endpoint.
    """
    if not getattr(a, "contract", None):
        return 0, "not requested"
    cmd = [sys.executable, "-u", os.path.join(HERE, "vmax_contract.py"),
           "check", "--baseline", os.path.abspath(a.contract)]
    for flag, val in (("--base", a.base), ("--redirect", a.redirect), ("--proxy", a.proxy)):
        if val:
            cmd += [flag, val]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return 2, "contract tool failed: %s" % str(e)[:120]
    detail = ""
    for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
        if line.strip():
            detail = line.strip()
    return p.returncode, detail[:200]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="one-card protocol experiments (xdevice / ttl / nosms)")
    ap.add_argument("mode", choices=["xdevice", "ttl", "nosms"])
    ap.add_argument("--phone", required=True, help="the card, 11 digits")
    ap.add_argument("--sms", default="http://127.0.0.1:8787/code?phone={phone}",
                    help="sms provider template (see vmax_sms.py)")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-timeout", type=int, default=300)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--invite", default="")
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--captcha", default="queue", choices=["auto", "manual", "assist", "param", "queue"])
    ap.add_argument("--captcha-param", default="")
    ap.add_argument("--captcha-attempts", type=int, default=6)
    ap.add_argument("--form-captcha", default="device",
                    choices=["auto", "reuse", "fresh", "device"],
                    help="captcha for POST /register: device (default) = the site's "
                         "own signup-check path -- empty param while trusted, 0 extra "
                         "tokens, with device_fallback/device_retry paying one when "
                         "the trust is refused; auto/fresh solve one; reuse never "
                         "re-solves")
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=600)
    ap.add_argument("--assist-dir", default="assist")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--debug-dir", default=None)
    ap.add_argument("--out", default="onecard.json", help="verdict JSON (no secrets)")
    ap.add_argument("--results", default=None,
                    help="append created accounts here (default: <out>.results.jsonl)")
    ap.add_argument("--no-exchange", dest="exchange", action="store_false",
                    help="do not exchange the 303 code for a token")
    ap.add_argument("--no-sms-check", action="store_true",
                    help="skip the pre-token channel probe")
    ap.add_argument("--force", action="store_true", help="keep going if the probe fails")
    ap.add_argument("--contract", default=None, metavar="BASELINE",
                    help="run vmax_contract.py check against this baseline before "
                         "the first send-code; drift/unavailable refuses to spend")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, spend nothing")
    ap.add_argument("--low", type=float, default=60.0, help="ttl: lower bound (seconds)")
    ap.add_argument("--high", type=float, default=600.0, help="ttl: upper bound (seconds)")
    ap.add_argument("--rounds", type=int, default=3, help="ttl: at most N probes (N SMS)")
    ap.add_argument("--cooldown", type=float, default=62.0,
                    help="ttl: seconds between send-code calls (server cooldown is 60)")
    ap.add_argument("--precision", type=float, default=30.0,
                    help="ttl: stop when hi-lo is below this")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    ns = ap.parse_args(argv)
    phone = normalize_phone(ns.phone)
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        print("[onecard] --phone must be an 11-digit mainland number "
              "(a virtual 170/171 segment will be refused by the carrier check)",
              file=sys.stderr)
        return 2
    ns.phone = phone
    if ns.results is None:
        base = ns.out if ns.out.lower().endswith(".json") else ns.out + ".json"
        ns.results = base[:-5] + ".results.jsonl"

    if ns.dry_run:
        print("[onecard] dry run -- nothing is probed, no SMS, no token", flush=True)
        if ns.mode == "xdevice":
            print("  xdevice: A send-code -> read code -> B /register with B's own "
                  "PKCE/state/captcha -> verdict", flush=True)
        elif ns.mode == "ttl":
            print("  ttl: probes at ~%s..%s s, up to %d SMS, cooldown %ss"
                  % (ns.low, ns.high, ns.rounds, ns.cooldown), flush=True)
        else:
            print("  nosms: send-code -> read code -> /register WITHOUT sms_code "
                  "(1 SMS; the form is free under device, else 1 token); if refused "
                  "for the value, retry with sms_code=\"\" (0 SMS, same form cost)",
                  flush=True)
        print("  phone  : %s" % mask(phone), flush=True)
        print("  form   : --form-captcha %s%s"
              % (ns.form_captcha,
                 " (empty captcha_verify_param while signup-check says false)"
                 if ns.form_captcha == "device" else ""), flush=True)
        print("  out    : %s" % ns.out, flush=True)
        print("  results: %s" % ns.results, flush=True)
        if ns.contract:
            print("  contract: %s" % os.path.abspath(ns.contract), flush=True)
        return 0

    # 0) the contract goes first: nothing single-use is spent on a drifted API
    rc_c, cdetail = contract_gate(ns)
    if rc_c != 0 and not ns.force:
        name = CONTRACT_DRIFT if rc_c == 1 else CONTRACT_UNAVAILABLE
        doc = {"tool": "vmax_onecard", "version": 1, "ts": int(time.time()),
               "mode": ns.mode,
               "contract": {"baseline": os.path.abspath(ns.contract), "exit": rc_c,
                            "detail": cdetail},
               "verdict": {"mode": ns.mode, "phone": phone, "answer": name,
                           "contract_exit": rc_c, "detail": cdetail,
                           "ts": int(time.time())}}
        d = os.path.dirname(os.path.abspath(ns.out))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(ns.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        print("[onecard] %s (contract exit %d: %s) -- nothing was spent"
              % (name, rc_c, cdetail), file=sys.stderr)
        return 4
    if rc_c != 0:
        print("[onecard] contract check exit %d; forced (%s)" % (rc_c, cdetail), flush=True)

    if not ns.no_sms_check:
        chk = channel_check(ns.sms, ns.sms_dir)
        print("[onecard] sms channel: %s -- %s"
              % ("OK" if chk["ok"] else "DOWN", chk["detail"]), flush=True)
        if not chk["ok"] and not ns.force:
            print("[onecard] refusing to spend on a dead channel "
                  "(fix it, or pass --no-sms-check / --force)", file=sys.stderr)
            return 9

    started = time.time()
    code = 0
    try:
        if ns.mode == "xdevice":
            rec = experiment_xdevice(ns)
        elif ns.mode == "ttl":
            rec = experiment_ttl(ns)
        else:
            rec = experiment_nosms(ns)
    except SmsTimeout as e:
        rec = {"mode": ns.mode, "phone": phone, "answer": "sms_timeout",
               "error": str(e)[:200], "ts": int(time.time())}
        code = 1
    except VmaxError as e:
        rec = {"mode": ns.mode, "phone": phone,
               "answer": "rate_limited" if e.status == 429 else "api_error",
               "status": e.status, "error": str(e)[:200], "ts": int(time.time())}
        code = 3 if e.status == 429 else 1
    rec["elapsed_s"] = round(time.time() - started, 1)
    doc = {"tool": "vmax_onecard", "version": 1, "ts": int(time.time()),
           "mode": ns.mode, "verdict": rec}
    if ns.contract:
        doc["contract"] = {"baseline": os.path.abspath(ns.contract), "exit": 0}

    if rec.get("answer") in (CODE_ACCEPTED, CODE_BOUND, "ttl_between",
                             "accepted_without_code", "accepted_with_empty_code",
                             "code_value_required", "no_pending_code_seen",
                             "schema_rejected"):
        code = 0
    elif rec.get("answer") == "rate_limited":
        code = 3
    else:
        # expired / unexpected / timeout: the experiment ran but did not answer
        code = 1

    d = os.path.dirname(os.path.abspath(ns.out))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(ns.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)

    print("[onecard] verdict: %s (%s) -> %s"
          % (rec.get("answer"), mask(phone), ns.out), flush=True)
    if rec.get("answer") == CODE_ACCEPTED:
        print("[onecard] the code followed the PHONE, not the session "
              "(PROTOCOL: cross-device redemption works)", flush=True)
    elif rec.get("answer") == CODE_BOUND:
        print("[onecard] the code is bound to the sending session "
              "(PROTOCOL: keep send+submit in one session)", flush=True)
    elif rec.get("answer") == "ttl_between":
        print("[onecard] code alive between %ss and %ss"
              % (rec["ttl_min_s"], rec["ttl_max_s"]), flush=True)
    elif rec.get("answer") in ("accepted_without_code", "accepted_with_empty_code"):
        print("[onecard] *** /register created the account without a usable sms_code *** "
              "the pending record alone was enough -- report this as a registration "
              "bypass (PROTOCOL §5.2.2)", flush=True)
    elif rec.get("answer") == "code_value_required":
        print("[onecard] the handler does compare sms_code: absent/empty is just "
              "a wrong code (PROTOCOL §5.2.2 closed)", flush=True)
    elif rec.get("answer") == "no_pending_code_seen":
        print("[onecard] /register did not see the code we had just sent "
              "(pending record scoped to something else?)", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())