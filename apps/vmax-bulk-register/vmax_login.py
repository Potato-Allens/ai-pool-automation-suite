# -*- coding: utf-8 -*-
"""Verify (and keep alive) existing accounts by password login -- no SMS, no browser.

    python vmax_login.py --input accounts.csv --out logins.jsonl \
        --captcha queue --queue-dir tokens
    python vmax_login.py --input accounts.csv --out logins.jsonl --save-token \
        --limit 5 --delay 2

Why this exists
---------------
The registration path ends at the OAuth token, which lives 3600 s (and costs a
real SMS to mint a fresh account).  Once an account exists, ``POST /api/auth/login``
re-issues a JWT from username + password + one captcha token -- no SIM involved
(openapi: 账号密码登录, 401 = wrong credentials, 403 = captcha required).  So a batch
can be verified ("does this account really work?") and refreshed on demand without
touching the number pool.

Cost: one captcha token per *successful* attempt.  Live probe (2026-10-26, production):
a wrong username/password answers **401 even with a bogus token**, i.e. credentials
are checked before the captcha -- so a row that ends in ``auth_failed`` did not
consume its token and the next row can reuse it (``--token-reuse-seconds``, default
60 s, 0 = never).  Only 200 / 403 / 429 and transport errors consume tokens.
The server takes either the username or the mainland phone number in the
``username`` field (openapi LoginRequest), so a row with only ``phone,password``
is still usable; such a row is marked ``username_used_phone: true``.

Statuses: ok / auth_failed / captcha_required / rate_limited / no_credentials /
transport / http_<code>.  ``auth_failed`` and ``no_credentials`` are terminal:
a resume does not retry them.

Input matters: when ``--input`` is a results.jsonl (any JSON/JSONL) only rows the
run actually registered (``status == "ok"``) are verified -- an account that was
never created has nothing to log into, and probing an already-registered number
with our generated password is a wrong-credential attempt against a stranger's
account (plus one wasted captcha token).  A plain cards CSV has no status column
and is verified in full, which is what ad-hoc/manual checks want.

Exit codes: 0 all ok, 1 some rows failed, 2 bad args, 3 hit 429 (stop, do not poll),
4 nothing to verify (every input row failed to register).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()

from vmax_batch import dedupe_accounts, load_accounts  # noqa: E402
from vmax_client import VmaxAuth  # noqa: E402
from vmax_register import normalize_phone, pop_token  # noqa: E402

TERMINAL = ("auth_failed", "no_credentials")
TOKEN_REUSE_SECONDS = 60.0


def classify(status: int, detail: str) -> str:
    text = str(detail or "")
    if status == 0:
        return "transport"
    if status == 429:
        return "rate_limited"
    if status == 403 or "人机验证" in text:
        return "captcha_required"
    if status == 401:
        return "auth_failed"
    return f"http_{status}" if status else "other"


def jwt_exp(token: str) -> int:
    """exp from a JWT payload, 0 when it cannot be read. Never verifies the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:  # noqa: BLE001
        return 0


def load_done(path: str) -> tuple[set, set]:
    """(phones already ok, phones that are terminally unusable) from a prior run."""
    ok, dead = set(), set()
    if not path or not os.path.exists(path):
        return ok, dead
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            ph = r.get("phone")
            if not ph:
                continue
            if r.get("status") == "ok":
                ok.add(ph)
            elif r.get("kind") in TERMINAL:
                dead.add(ph)
    return ok, dead


def registered(path: str) -> dict[str, dict] | None:
    """phone -> {username, password} for rows with status=ok.

    Returns None when the input carries no status column at all (a plain
    ``cards.csv``), meaning "verify every row" -- the old behaviour.  For a
    results.jsonl the ok row is also the authoritative source of credentials:
    a retried phone can appear twice, and the failing attempt's password is not
    the one that was used to create the account.
    """
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        head = f.read(4096)
    if not (path.lower().endswith(".jsonl") or head.lstrip().startswith("{")):
        return None
    out: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            phone = normalize_phone(str(r.get("phone") or ""))
            if phone and r.get("status") == "ok":
                out[phone] = {"username": r.get("username") or None,
                              "password": r.get("password") or None}
    return out


def take_param(args, holder):
    """(captcha param, reused?) -- reuse a token the server provably never consumed.

    A 401 (wrong credentials) happens before the captcha is validated, so that
    token is still good for the next row; reusing it saves a solve per bad password.
    The window is capped (well under the 90-120 s token TTL) so a stale token can
    never turn a good account into a spurious 403.
    """
    if args.captcha == "param":
        return args.captcha_param, False
    window = float(getattr(args, "token_reuse_seconds", TOKEN_REUSE_SECONDS) or 0)
    if holder and holder.get("param") and window > 0:
        if time.time() - float(holder.get("ts") or 0) <= window:
            return holder["param"], True
        holder["param"] = ""
    param = pop_token(args.queue_dir, args.queue_wait)
    if holder is not None:
        holder["param"], holder["ts"] = param, time.time()
        holder["popped"] = int(holder.get("popped") or 0) + 1
    return param, False


def one(cli: VmaxAuth, acc: dict, args, holder=None) -> dict:
    phone = normalize_phone(acc.get("phone") or "")
    res = {"phone": phone, "ts": int(time.time())}
    password = acc.get("password") or ""
    username = acc.get("username") or ""
    used_phone = False
    if not username and password and phone:
        # openapi LoginRequest: "使用用户名或中国大陆手机号验证密码" -- the server
        # accepts the phone number in the username field, so an account imported
        # from elsewhere can still be verified with phone + password only.
        username = phone
        used_phone = True
        res["username_used_phone"] = True
    res["username"] = username
    if not username or not password:
        res.update(status="error", kind="no_credentials",
                   error="row has no password (username is optional: the phone works too)")
        return res
    try:
        param, reused = take_param(args, holder)
    except RuntimeError as e:
        res.update(status="error", kind="no_token", error=str(e)[:300])
        return res
    res["token_reused"] = reused
    try:
        out = cli.login_password(username, password, param, platform=args.platform)
    except Exception as e:  # noqa: BLE001 - one bad row must not kill the batch
        res.update(status="error", kind="transport", token_spent=True,
                   error=f"{type(e).__name__}: {e}"[:300])
        if holder is not None:
            holder["param"] = ""      # unknown server state: never reuse
        return res
    status = int(out.get("status") or 0)
    tok = str(out.get("access_token") or "")
    user = out.get("user") or {}
    if status == 200 and tok:
        res["token_spent"] = True
        if holder is not None:
            holder["param"] = ""      # consumed by a successful login
        exp = jwt_exp(tok)
        res.update(status="ok", uid=user.get("uid"),
                   username_masked=user.get("username"),
                   phone_masked=user.get("phone"),
                   area_type=user.get("area_type"),
                   expires_at=exp,
                   expires_in=(exp - int(time.time())) if exp else None,
                   token_masked=tok[:12] + "..." + tok[-6:])
        if args.save_token:
            res["access_token"] = tok
        return res
    kind = classify(status, out.get("detail") or out.get("message") or "")
    if kind == "auth_failed":
        # 401 = credentials checked first, the token was never consumed -> keep it
        res["token_spent"] = False
    else:
        res["token_spent"] = True
        if holder is not None:
            holder["param"] = ""      # 403/429/...: the token is gone or unproven
    res.update(status="error", kind=kind, http=status,
               error=str(out.get("detail") or out.get("message") or "")[:300])
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="verify accounts by password login (no SMS)")
    ap.add_argument("--input", required=True, help="CSV / JSONL with phone,username,password")
    ap.add_argument("--out", default="logins.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between attempts")
    ap.add_argument("--max-fail", type=int, default=0, help="stop after N real failures")
    ap.add_argument("--captcha", default="queue", choices=["queue", "param"])
    ap.add_argument("--captcha-param", default="")
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=600)
    ap.add_argument("--token-reuse-seconds", type=float, default=TOKEN_REUSE_SECONDS,
                    help="reuse a token that a 401 proved unconsumed (0 = never); keep it "
                         "well under the 90-120 s token TTL")
    ap.add_argument("--save-token", action="store_true",
                    help="also store the JWT in the output (it is a live credential)")
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--proxy", default=None)
    args = ap.parse_args(argv)

    # load_accounts() returns a plain list; dedupe_accounts() collapses repeated
    # phones and reports how many it dropped (a second attempt on the same number
    # would only burn another captcha token).
    accounts, dups = dedupe_accounts(load_accounts(args.input))
    if not accounts:
        print(f"[login] no rows in {args.input}", flush=True)
        return 2
    if dups:
        print(f"[login] collapsed {dups} duplicate phone row(s)", flush=True)
    reg = registered(args.input)
    if reg is not None:
        kept = []
        for a in accounts:
            r = reg.get(a["phone"])
            if r is None:
                continue
            merged = dict(a)
            # The status=ok row is authoritative for the credentials -- including a
            # *null* username.  A password-reset row has username=None (the phone is
            # the login name), while the failed register attempt that produced the
            # first row for this phone carries a generated handle that was never
            # created server-side; keeping it turned a working account into a bogus
            # auth_failed (caught by test_day_e2e [5]).
            merged["username"] = r.get("username") or None
            if r.get("password"):
                merged["password"] = r["password"]
            kept.append(merged)
        dropped = len(accounts) - len(kept)
        if dropped:
            print(f"[login] {dropped} row(s) never registered (status != ok) -> "
                  f"not verified (no account to log into, no token spent)", flush=True)
        accounts = kept
        if not accounts:
            print("[login] nothing to verify: no row in the input has an account", flush=True)
            return 4
    done, dead = load_done(args.out)
    todo = [a for a in accounts
            if normalize_phone(a.get("phone") or "") not in done
            and normalize_phone(a.get("phone") or "") not in dead]
    if args.limit:
        todo = todo[: args.limit]
    print(f"[login] {len(accounts)} rows, {len(done)} ok, {len(dead)} unusable, "
          f"{len(todo)} to check", flush=True)
    if not todo:
        print("[login] nothing to do", flush=True)
        return 0

    cli = VmaxAuth(proxy=args.proxy, base=args.base, redirect_uri=args.redirect)
    ok = fail = 0
    kinds: dict[str, int] = {}
    tokens = 0
    by_phone_name = 0
    aborted = False
    started = time.time()
    holder = {"param": "", "ts": 0.0, "popped": 0}
    reused_any = 0
    with open(args.out, "a", encoding="utf-8") as fh:
        for i, acc in enumerate(todo, 1):
            res = one(cli, acc, args, holder)
            if res.get("token_reused"):
                reused_any += 1
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")
            fh.flush()
            if res.get("status") == "ok":
                ok += 1
                print(f"[login] {i}/{len(todo)} {res['phone']} ok uid={res.get('uid')} "
                      f"expires_in={res.get('expires_in')}s", flush=True)
            else:
                fail += 1
                kind = res.get("kind") or "error"
                kinds[kind] = kinds.get(kind, 0) + 1
                print(f"[login] {i}/{len(todo)} {res['phone']} {kind}: "
                      f"{str(res.get('error'))[:80]}", flush=True)
                if kind == "rate_limited":
                    aborted = True
                    print("[login] 429 -> stopping (do not poll; the ban renews)", flush=True)
                    break
                if args.max_fail and fail >= args.max_fail:
                    print(f"[login] --max-fail {args.max_fail} reached; stopping", flush=True)
                    break
            if res.get("token_spent"):
                tokens += 1
            if res.get("username_used_phone"):
                by_phone_name += 1
            if args.delay:
                time.sleep(args.delay)

    summary = {"ts": int(time.time()), "elapsed_s": round(time.time() - started, 1),
               "input": os.path.abspath(args.input), "out": os.path.abspath(args.out),
               "rows": len(todo), "ok": ok, "fail": fail, "by_kind": kinds,
               "captcha_tokens_spent": tokens, "tokens_popped": holder["popped"],
               "tokens_reused": reused_any, "aborted_on_429": aborted,
               "phone_as_username": by_phone_name,
               "tokens_stored": bool(args.save_token)}
    sp = args.out + ".summary.json"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"[login] ok={ok} fail={fail} aborted={aborted} "
          f"tokens~{summary['captcha_tokens_spent']} -> {sp}", flush=True)
    if aborted:
        return 3
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
