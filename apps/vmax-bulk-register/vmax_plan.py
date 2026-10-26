# -*- coding: utf-8 -*-
"""Capacity planner built from the measured constraints (no network, no SMS).

    python vmax_plan.py --target 100
    python vmax_plan.py --target 500 --solvers 3 --cards 4 --json
    python vmax_plan.py --target 500 --deadline-hours 6      # what do I need?

Every default is a measured constant (docs/PROTOCOL.md, docs/RUNBOOK.md);
override any of them to see how the plan moves.

Constraints modelled
--------------------
tokens    a captcha token is single-use and lives 90-120 s; the solver passes ~35%
          of challenges, so one solver yields 3600 * pass / solve_seconds tokens/h
writes    a fresh IP survives a 7-write 3 s burst, but ~11 writes in 18 min trips a
          429 that outlived 27 min of silence (R156: the bucket is keyed on the egress
          IP), so the plan is paced at 30 writes/h/IP -- not the 255/h burst
sms       one message per attempt from a real SIM (cards * 3600 / cycle)
workers   one in-flight registration per worker, dominated by the SMS wait
pool      recycled / shared numbers are partly already registered.  In the default
          skip mode such a number burns one single-use token and no SMS/write, so the
          token wall moves by 1/(1-taken_rate).  With --reset-taken (the runner's
          --on-taken reset) it is *recovered* instead: one more token and one SMS per
          rescue, the second reset step is captcha-free and is not a /register write,
          so you stop buying extra cards to cover the taken fraction
"""
from __future__ import annotations

import argparse
import json
import math

import vmax_formcost as formcost  # noqa: E402  (same dir; the price list lives there)
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()

# Measured defaults (2026-10-26).
PASS_RATE = 0.35          # slider, 21/60 single-try
SOLVE_SECONDS = 20.0      # one challenge, end to end
TOKEN_TTL = 105.0         # seconds; measured 90-120
WRITE_CAP = 5             # burst: /register writes per window, per egress IP
WRITE_WINDOW = 60.0       # the burst wall only: 5/60 s = 255 writes/h, never sustainable
#: Sustained writes *per egress IP* -- the number that binds a long run.
#: R75/R154/R156: a fresh bucket takes 7 writes in 3 s (the 8th is 429), but one IP
#: that spent 11 writes over ~18 min was refused, and `_r154d_scope.json` shows a
#: *new device + new phone* from that IP still 429 at +27 min of silence -> the key
#: is the egress IP.  30/h sits below the measured ~37/h that tripped.
WRITE_PER_HOUR = 30.0
SMS_SECONDS = 45.0        # send-code -> message on a real SIM
ACCOUNT_SECONDS = 50.0    # wall clock for one account incl. SMS wait
ERROR_RATE = 0.15         # wrong/expired code on the first try
MAX_FAIL = 5              # attempts before the number is abandoned


def token_rate_per_hour(solvers: float, pass_rate: float, solve_seconds: float) -> float:
    """Tokens one or more solvers can mint per hour."""
    if solvers <= 0 or solve_seconds <= 0:
        return 0.0
    return solvers * 3600.0 * pass_rate / solve_seconds


#: The policy names the runners accept (vmax_formcost knows the price of each).
FORM_POLICIES = ("auto", "reuse", "fresh", "device")


def form_tokens_per_write(form_captcha: str = "fresh", device_trust: float = 1.0) -> float:
    """Tokens bought for POST /register, per write, under one ``--form-captcha`` policy.

    ``fresh`` buys one token every time -- the conservative default, and what the
    planner assumed everywhere before R153.  ``device`` rides the ``auth_device``
    trust, so it only pays for ``1 - device_trust`` of the writes.

    ``auto`` / ``reuse`` used to be free here (R153).  R154 measured why that was
    wrong: the send-code token is *consumed* by the verify that sends the SMS, so a
    form submit carrying it is rejected and ``auto`` always pays for the retry.
    ``reuse`` never buys the retry, therefore it cannot deliver an account at all --
    planning it is refused instead of being priced at zero.

    Kept conservative on purpose: the free path (``device``) is the *runner's*
    default, but a budget that quietly assumes it is a budget that lies the first
    time the trust is refused.  ``fresh`` stays the default until a real card
    proves the others end to end.
    """
    kind = (form_captcha or "fresh").strip().lower()
    if kind in ("fresh", "auto"):
        # R154: /register/send-code *consumes* the Aliyun cert (a second verify with
        # the same param answers "Captcha verification rejected" while a fresh one
        # answers the 59 s phone cooldown), so ``auto``'s reuse attempt is followed
        # by the retry every time -- one bought token per form write, plus one
        # wasted /register write that this function does not model.
        return 1.0
    if kind == "reuse":
        raise ValueError("form policy 'reuse' cannot deliver an account: the token was "
                         "already spent by send-code (R154); use 'fresh' or 'device'")
    if kind == "device":
        trust = min(max(float(device_trust), 0.0), 1.0)
        return 1.0 - trust
    raise ValueError("unknown form policy: %r (want one of %s)"
                     % (form_captcha, ", ".join(FORM_POLICIES)))


def writes_per_account(error_rate: float, max_fail: int = MAX_FAIL) -> float:
    """Expected /register writes per account with geometric retries, capped."""
    p = min(max(float(error_rate), 0.0), 0.999)
    n = max(1, int(max_fail))
    if p <= 0.0:
        return 1.0
    return (1.0 - p ** n) / (1.0 - p)


def tokens_per_account(error_rate: float, max_fail: int = MAX_FAIL,
                       taken_rate: float = 0.0, form_captcha: str = "fresh",
                       device_trust: float = 1.0) -> float:
    """Tokens burnt per *delivered* account.

    One for send-code plus one per /register write (tokens are single-use).  When a
    share of the pool is already registered, those numbers still consume their
    send-code token before being refused -- so buying ``target / (1 - taken_rate)``
    numbers costs ``1 / (1 - taken_rate)`` tokens per delivered account, while SMS
    and writes stay at one account's worth (a taken number never gets either).
    """
    y = max(1e-6, 1.0 - min(max(float(taken_rate), 0.0), 0.999999))
    per_write = form_tokens_per_write(form_captcha, device_trust)
    return 1.0 / y + writes_per_account(error_rate, max_fail) * per_write


def pool_cost(error_rate: float, max_fail: int = MAX_FAIL, taken_rate: float = 0.0,
              reset_taken: bool = False, reset_ok: float = 1.0,
              form_captcha: str = "fresh", device_trust: float = 1.0) -> dict:
    """Cost per *delivered* account for a pool that is ``taken_rate`` registered.

    ``reset_taken=False`` is the skip mode: a taken number is dead weight (one token,
    no SMS, no write), so the pool penalty is ``1/(1-t)`` on numbers and tokens.

    ``reset_taken=True`` mirrors ``--on-taken reset``: every taken number is retried
    through the password-reset flow -- one more token for send-code, one SMS if the
    code is delivered, and the captcha-free second step is not a /register write.
    ``reset_ok`` is the share of rescues that end in an account.  A rescued number
    still costs 2 tokens, which is *cheaper* than buying a second card for it.
    """
    t = min(max(float(taken_rate), 0.0), 0.999999)
    ok = min(max(float(reset_ok), 0.0), 1.0)
    fresh = 1.0 - t
    w = writes_per_account(error_rate, max_fail)
    per_write = form_tokens_per_write(form_captcha, device_trust)
    form_cost = w * per_write
    if not reset_taken:
        y = max(1e-6, fresh)
        return {"taken_rate": t, "reset_ok": ok, "yield": y,
                "tokens": 1.0 / y + form_cost, "sms": w, "writes": w,
                "form_tokens_per_write": per_write,
                "phones_per_account": 1.0 / y}
    y = max(1e-6, fresh + t * ok)
    return {"taken_rate": t, "reset_ok": ok, "yield": y,
            "tokens": (fresh * (1.0 + form_cost) + 2.0 * t) / y,
            "sms": (fresh * w + t * ok) / y,
            "writes": fresh * w / y,
            "form_tokens_per_write": per_write,
            "phones_per_account": 1.0 / y}


def ceilings(solvers: float = 1.0, pass_rate: float = PASS_RATE,
             solve_seconds: float = SOLVE_SECONDS, error_rate: float = ERROR_RATE,
             max_fail: int = MAX_FAIL, write_cap: int = WRITE_CAP,
             write_window: float = WRITE_WINDOW, write_per_hour: float = WRITE_PER_HOUR,
             cards: float = 1.0,
             sms_seconds: float = SMS_SECONDS, workers: float = 2.0,
             account_seconds: float = ACCOUNT_SECONDS, taken_rate: float = 0.0,
             reset_taken: bool = False, reset_ok: float = 1.0,
             form_captcha: str = "fresh", device_trust: float = 1.0,
             ips: float = 1.0) -> dict:
    """Accounts/hour each constraint allows on its own.

    ``ips`` is how many egress IPs (distinct proxies) the fleet runs.  Only the
    ``writes`` wall scales with it -- every other constraint is per account, not
    per IP.  R156: one IP sustains ~30 writes/h, so the second IP is where the
    plan stops being write-bound; a 0-IP fleet can deliver nothing, by design.
    """
    c = pool_cost(error_rate, max_fail, taken_rate, reset_taken, reset_ok,
                  form_captcha=form_captcha, device_trust=device_trust)
    w, t = c["writes"], c["tokens"]
    tok = token_rate_per_hour(solvers, pass_rate, solve_seconds) / t
    wr_burst = write_cap * 3600.0 / max(1.0, write_window) / w
    wr_sustained = max(1e-9, float(write_per_hour)) / w
    wr = max(0.0, float(ips)) * min(wr_burst, wr_sustained)
    sms = cards * 3600.0 / max(1.0, sms_seconds)
    wrk = workers * 3600.0 / max(1.0, account_seconds)
    return {"tokens": tok, "writes": wr, "sms": sms, "workers": wrk}


ADVICE = {
    "tokens": "add solvers (or lower solve_seconds): tokens are single-use, this is the usual wall",
    "writes": "you are at the 429 ceiling (~30 writes/h per egress IP, R156): fewer retries or more IPs",
    "sms": "add SIM cards / forwarders: one card cannot feed more than a message per cycle",
    "workers": "raise --workers (or lower the per-account wall clock)",
}


def plan(target: int, solvers: float = 1.0, pass_rate: float = PASS_RATE,
         solve_seconds: float = SOLVE_SECONDS, error_rate: float = ERROR_RATE,
         max_fail: int = MAX_FAIL, write_cap: int = WRITE_CAP,
         write_window: float = WRITE_WINDOW, write_per_hour: float = WRITE_PER_HOUR,
         cards: float = 1.0,
         sms_seconds: float = SMS_SECONDS, workers: float = 2.0,
         account_seconds: float = ACCOUNT_SECONDS, token_ttl: float = TOKEN_TTL,
         taken_rate: float = 0.0, reset_taken: bool = False,
         reset_ok: float = 1.0, form_captcha: str = "fresh",
         device_trust: float = 1.0, ips: float = 1.0) -> dict:
    lim = ceilings(solvers, pass_rate, solve_seconds, error_rate, max_fail, write_cap,
                   write_window, write_per_hour, cards, sms_seconds, workers, account_seconds,
                   taken_rate=taken_rate, reset_taken=reset_taken, reset_ok=reset_ok,
                   form_captcha=form_captcha, device_trust=device_trust, ips=ips)
    binding = min(lim, key=lambda k: lim[k])
    rate = lim[binding]
    c = pool_cost(error_rate, max_fail, taken_rate, reset_taken, reset_ok,
                  form_captcha=form_captcha, device_trust=device_trust)
    w, t, y = c["writes"], c["tokens"], c["yield"]
    hours = (target / rate) if rate > 0 else None
    return {
        "target": int(target),
        "per_hour": {k: round(v, 2) for k, v in lim.items()},
        "binding": binding,
        "advice": ADVICE[binding],
        "rate": round(rate, 2),
        "hours": (round(hours, 2) if hours is not None else None),
        "writes_per_account": round(w, 3),
        "tokens_per_account": round(t, 3),
        "form_captcha": (form_captcha or "fresh").strip().lower(),
        "form_tokens_per_write": round(c["form_tokens_per_write"], 3),
        "tokens_total": round(target * t, 1),
        "sms_total": round(target * c["sms"], 1),
        "sms_per_account": round(c["sms"], 3),
        "taken_rate": round(min(max(float(taken_rate), 0.0), 0.999999), 3),
        "reset_taken": bool(reset_taken),
        "reset_ok": round(min(max(float(reset_ok), 0.0), 1.0), 3),
        "yield": round(y, 3),
        "phones_needed": int(math.ceil(target / y)),
        "queue_cap": round(rate * token_ttl / 3600.0, 1),
        "queue_seconds": int(token_ttl),
        "write_per_hour": round(float(write_per_hour), 1),
        "ips": int(ips),
        "inputs": {
            "solvers": solvers, "pass_rate": pass_rate, "solve_seconds": solve_seconds,
            "error_rate": error_rate, "max_fail": max_fail, "write_cap": write_cap,
            "write_window": write_window, "write_per_hour": write_per_hour,
            "ips": int(ips), "cards": cards, "sms_seconds": sms_seconds,
            "workers": workers, "account_seconds": account_seconds,
            "taken_rate": taken_rate, "reset_taken": bool(reset_taken),
            "reset_ok": reset_ok, "form_captcha": (form_captcha or "fresh").strip().lower(),
            "device_trust": device_trust,
        },
    }


def needs(target: int, hours: float, **kw) -> dict:
    """What it takes to finish `target` accounts inside `hours`."""
    if hours <= 0:
        raise ValueError("hours must be > 0")
    rate = target / hours
    taken = kw.get("taken_rate", 0.0) or 0.0
    c = pool_cost(kw.get("error_rate", ERROR_RATE), kw.get("max_fail", MAX_FAIL), taken,
                  kw.get("reset_taken", False), kw.get("reset_ok", 1.0),
                  form_captcha=kw.get("form_captcha", "fresh"),
                  device_trust=kw.get("device_trust", 1.0))
    w, t, y = c["writes"], c["tokens"], c["yield"]
    per_solver = token_rate_per_hour(1.0, kw.get("pass_rate", PASS_RATE),
                                     kw.get("solve_seconds", SOLVE_SECONDS)) or 1.0
    solvers = math.ceil(rate * t / per_solver)
    cards = math.ceil(rate * kw.get("sms_seconds", SMS_SECONDS) / 3600.0)
    workers = math.ceil(rate * kw.get("account_seconds", ACCOUNT_SECONDS) / 3600.0)
    writes_h = rate * w
    per_ip = min(kw.get("write_cap", WRITE_CAP) * 3600.0 / max(1.0, kw.get("write_window", WRITE_WINDOW)),
                 max(1e-9, float(kw.get("write_per_hour", WRITE_PER_HOUR))))
    ips = max(1.0, float(kw.get("ips", 1.0)))
    cap_h = per_ip * ips
    return {
        "target": int(target), "hours": hours, "accounts_per_hour": round(rate, 2),
        "solvers": solvers, "cards": cards, "workers": workers,
        "writes_per_hour": round(writes_h, 1), "write_cap_per_hour": round(cap_h, 1),
        "write_cap_ok": writes_h <= cap_h,
        "ips_needed": int(math.ceil(writes_h / per_ip)),
        "write_cap_per_ip": round(per_ip, 1),
        "ips": int(ips),
        "tokens_total": round(target * t, 1),
        "tokens_per_account": round(t, 3),
        "form_captcha": kw.get("form_captcha", "fresh"),
        "form_tokens_per_write": round(c["form_tokens_per_write"], 3),
        "sms_total": round(target * c["sms"], 1),
        "sms_per_account": round(c["sms"], 3),
        "phones_needed": int(math.ceil(target / y)),
        "taken_rate": round(min(max(float(taken), 0.0), 0.999999), 3),
        "reset_taken": bool(kw.get("reset_taken", False)),
    }


def render(p: dict) -> list:
    out = [f"CAPACITY PLAN  target={p['target']} accounts"]
    out.append("")
    out.append(f"  {'constraint':<10}  {'accounts/h':>11}  note")
    notes = {
        "tokens": "captcha tokens (single-use, ~105 s ttl)",
        "writes": f"/register 429 bucket ({p['ips']} egress IP"
                  f" x {p['write_per_hour']:.0f} writes/h)",
        "sms": "real SIM messages",
        "workers": "in-flight registrations",
    }
    for k in ("tokens", "writes", "sms", "workers"):
        mark = "  <-- binding" if k == p["binding"] else ""
        out.append(f"  {k:<10}  {p['per_hour'][k]:>11.1f}  {notes[k]}{mark}")
    out.append("")
    out.append(f"  rate     {p['rate']:.1f} accounts/h  (limited by {p['binding']})")
    if p["hours"] is None:
        out.append("  eta      never -- that constraint is at zero")
    else:
        out.append(f"  eta      {p['hours']:.2f} h for {p['target']} accounts")
    out.append(f"  tokens   {p['tokens_per_account']:.2f}/account -> {p['tokens_total']:.0f} total"
               f"  (queue cap {p['queue_cap']:.1f} at ttl {p['queue_seconds']} s)")
    _note = ("  (+1 wasted /register write: send-code already spent that token)"
             if p["form_captcha"] == "auto" else "")
    out.append(f"  form     policy={p['form_captcha']} "
               f"{p['form_tokens_per_write']:.2f} token/write for /register{_note}")
    out.append(f"  sms      {p['sms_total']:.0f} messages"
               f"  (writes/account {p['writes_per_account']:.2f})")
    if p.get("taken_rate") and p.get("reset_taken"):
        out.append(f"  pool     {p['taken_rate']:.0%} of numbers already registered,"
                   f" recovered by password reset  -> buy {p['phones_needed']} numbers"
                   f" for {p['target']} (yield {p['yield']:.0%};"
                   f" 2 tokens + 1 SMS each, no /register write)")
    elif p.get("taken_rate"):
        out.append(f"  pool     {p['taken_rate']:.0%} of numbers already registered"
                   f"  -> buy {p['phones_needed']} numbers for {p['target']}"
                   f" (yield {p['yield']:.0%}; taken ones cost 1 token, 0 SMS)")
    if p.get("ips", 1) > 1:
        out.append(f"  ips      {p['ips']} egress IPs x {p['write_per_hour']:.0f} writes/h"
                   f" = {p['ips'] * p['write_per_hour']:.0f} writes/h of write budget")
    out.append(f"  advice   {p['advice']}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="capacity planner from measured constraints")
    ap.add_argument("--target", type=int, default=100, help="accounts you want")
    ap.add_argument("--solvers", type=float, default=1.0, help="captcha solver workers")
    ap.add_argument("--pass-rate", type=float, default=PASS_RATE)
    ap.add_argument("--solve-seconds", type=float, default=SOLVE_SECONDS)
    ap.add_argument("--error-rate", type=float, default=ERROR_RATE,
                    help="wrong/expired code on the first try")
    ap.add_argument("--max-fail", type=int, default=MAX_FAIL)
    ap.add_argument("--write-cap", type=int, default=WRITE_CAP)
    ap.add_argument("--write-window", type=float, default=WRITE_WINDOW)
    ap.add_argument("--write-per-hour", type=float, default=WRITE_PER_HOUR,
                    help="sustained /register writes one egress IP gets before 429 "
                         "(R154: 11 in ~18 min, then a 30+ min ban; default 30)")
    ap.add_argument("--ips", type=int, default=1,
                    help="egress IPs (= distinct proxies) in the fleet; only the write "
                         "wall scales with it (R156: 30 writes/h per IP)")
    ap.add_argument("--cards", type=float, default=1.0, help="SIM cards / forwarders")
    ap.add_argument("--sms-seconds", type=float, default=SMS_SECONDS)
    ap.add_argument("--workers", type=float, default=2.0)
    ap.add_argument("--account-seconds", type=float, default=ACCOUNT_SECONDS)
    ap.add_argument("--token-ttl", type=float, default=TOKEN_TTL)
    ap.add_argument("--taken-rate", type=float, default=0.0,
                    help="share of the number pool that is already registered (0-0.99); "
                         "those burn one token at send-code but no SMS and no write")
    ap.add_argument("--reset-taken", action="store_true",
                    help="already-registered numbers are recovered with the password-reset "
                         "flow (2 tokens + 1 SMS each) instead of being thrown away")
    ap.add_argument("--reset-ok", type=float, default=1.0,
                    help="share of reset rescues that yield an account (default 1.0)")
    ap.add_argument("--form-captcha", default="fresh", choices=list(FORM_POLICIES),
                    help="what POST /register costs in tokens: fresh = one per write "
                         "(conservative, the old model); auto = the send-code token is "
                         "spent, so the retry pays one per write (+1 wasted write); "
                         "reuse = refused, it cannot deliver (R154); device = the "
                         "auth_device trust covers 1 - --device-trust of the writes")
    ap.add_argument("--device-trust", type=float, default=1.0,
                    help="share of /register writes the device path covers under "
                         "--form-captcha device (default 1.0: measured R140/R144)")
    ap.add_argument("--deadline-hours", type=float, default=None,
                    help="instead of an ETA, report what is needed to finish in N hours")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.target <= 0:
        print("[plan] --target must be > 0", flush=True)
        return 2
    if not 0.0 < a.reset_ok <= 1.0:
        print("[plan] --reset-ok must be in (0, 1]", flush=True)
        return 2
    if not 0.0 <= a.device_trust <= 1.0:
        print("[plan] --device-trust must be in [0, 1]", flush=True)
        return 2
    if a.ips < 1:
        print("[plan] --ips must be >= 1", flush=True)
        return 2
    if a.write_per_hour <= 0:
        print("[plan] --write-per-hour must be > 0", flush=True)
        return 2
    if a.write_window <= 0:
        print("[plan] --write-window must be > 0", flush=True)
        return 2
    if not 0.0 <= a.taken_rate < 1.0:
        print("[plan] --taken-rate must be in [0, 1) -- 1.0 would never deliver an account",
              flush=True)
        return 2
    kw = dict(solvers=a.solvers, pass_rate=a.pass_rate, solve_seconds=a.solve_seconds,
              error_rate=a.error_rate, max_fail=a.max_fail, write_cap=a.write_cap,
              write_window=a.write_window, write_per_hour=a.write_per_hour,
              ips=a.ips, cards=a.cards, sms_seconds=a.sms_seconds,
              workers=a.workers, account_seconds=a.account_seconds,
              taken_rate=a.taken_rate, reset_taken=a.reset_taken, reset_ok=a.reset_ok,
              form_captcha=a.form_captcha, device_trust=a.device_trust)
    if a.deadline_hours is not None:
        if a.deadline_hours <= 0:
            print("[plan] --deadline-hours must be > 0", flush=True)
            return 2
        try:
            need = needs(a.target, a.deadline_hours, **kw)
        except ValueError as e:
            print(f"[plan] {e}", flush=True)
            return 2
        if a.json:
            print(json.dumps(need, ensure_ascii=False, indent=1))
        else:
            print(f"TO FINISH {need['target']} IN {need['hours']}h "
                  f"({need['accounts_per_hour']}/h)")
            print(f"  solvers  {need['solvers']}")
            print(f"  cards    {need['cards']}")
            print(f"  workers  {need['workers']}")
            print(f"  writes   {need['writes_per_hour']}/h vs cap {need['write_cap_per_hour']}/h"
                  + ("" if need["write_cap_ok"] else "   <-- OVER THE 429 CEILING"))
            print(f"  ips      {need['ips_needed']} egress IP(s) needed"
                  f" at {need['write_cap_per_ip']}/h each"
                  + (f"   (fleet: {need['ips']})" if need.get("ips", 1) > 1 else ""))
            print(f"  tokens   {need['tokens_total']:.0f}   sms {need['sms_total']:.0f}"
                  f"   phones {need['phones_needed']}"
                  + (f"  (pool {need['taken_rate']:.0%} taken)" if need["taken_rate"] else ""))
        return 0

    try:
        p = plan(a.target, token_ttl=a.token_ttl, **kw)
    except ValueError as e:
        print(f"[plan] {e}", flush=True)
        return 2
    if a.json:
        print(json.dumps(p, ensure_ascii=False, indent=1))
    else:
        for line in render(p):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
