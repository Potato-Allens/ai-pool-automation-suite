# -*- coding: utf-8 -*-
"""Offline checks for the capacity planner (pure arithmetic, no network)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLKIT = os.path.dirname(HERE)
sys.path.insert(0, TOOLKIT)

import vmax_plan as P  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def close(a, b, tol=0.01):
    return abs(a - b) <= tol


def main():
    print("[1] token rate")
    check("one solver 63/h", close(P.token_rate_per_hour(1, 0.35, 20), 63.0),
          P.token_rate_per_hour(1, 0.35, 20))
    check("two solvers double", close(P.token_rate_per_hour(2, 0.35, 20), 126.0))
    check("zero solvers -> 0", P.token_rate_per_hour(0, 0.35, 20) == 0.0)
    check("zero solve_seconds -> 0", P.token_rate_per_hour(1, 0.35, 0) == 0.0)
    check("pass rate scales linearly", close(P.token_rate_per_hour(1, 0.7, 20), 126.0))

    print("[2] writes per account (geometric retries, capped)")
    check("no errors -> 1.0", close(P.writes_per_account(0.0), 1.0))
    check("0.2 / 5 tries", close(P.writes_per_account(0.2, 5), 1.2496, 1e-4),
          P.writes_per_account(0.2, 5))
    check("max_fail=1 -> 1.0", close(P.writes_per_account(0.9, 1), 1.0))
    check("capped at max_fail", close(P.writes_per_account(0.999, 5), 4.995, 0.01),
          P.writes_per_account(0.999, 5))
    check("err=1.0 clamps, stays finite", P.writes_per_account(1.0, 3) <= 3.0 + 1e-9,
          P.writes_per_account(1.0, 3))
    check("tokens = 1 + writes", close(P.tokens_per_account(0.2, 5), 2.2496, 1e-4))

    print("[3] ceilings with measured defaults")
    lim = P.ceilings()
    check("tokens 28.9/h", close(lim["tokens"], 28.9, 0.05), lim["tokens"])
    check("writes 25.5/h at the sustained 30/h (R156)",
          close(lim["writes"], 25.5, 0.05), lim["writes"])
    check("burst-only ceiling is still 255/h",
          close(P.ceilings(write_per_hour=1e9)["writes"], 255.0, 0.05),
          P.ceilings(write_per_hour=1e9)["writes"])
    check("the burst scales with --write-cap (10/60 s -> 510/h)",
          close(P.ceilings(write_cap=10, write_per_hour=1e9)["writes"], 510.0, 0.05))
    check("a huge burst window cannot lift the sustained cap",
          close(P.ceilings(write_cap=1000)["writes"], 25.5, 0.05))
    check("sms 80/h for one card", close(lim["sms"], 80.0, 0.05), lim["sms"])
    check("workers 144/h", close(lim["workers"], 144.0, 0.05), lim["workers"])
    worse = P.ceilings(error_rate=0.4)
    check("more errors -> fewer accounts/h", worse["tokens"] < lim["tokens"], worse)
    check("more errors -> tighter write ceiling", worse["writes"] < lim["writes"], worse)

    print("[4] binding constraint")
    p = P.plan(100)
    check("writes bind by default (R156: 30 writes/h/IP)", p["binding"] == "writes",
          p["binding"])
    check("rate matches ceiling", close(p["rate"], lim["writes"], 0.05), p["rate"])
    check("eta = target/rate", close(p["hours"], 100 / lim["writes"], 0.01), p["hours"])
    p_tok = P.plan(100, write_per_hour=300)
    check("a 300/h write budget puts tokens back on top", p_tok["binding"] == "tokens",
          p_tok["per_hour"])
    check("... at the measured 28.95/h token wall", close(p_tok["rate"], 28.95, 0.01),
          p_tok["rate"])
    check("tokens_total", close(p["tokens_total"], 100 * p["tokens_per_account"], 0.2),
          p["tokens_total"])
    check("sms_total", close(p["sms_total"], 100 * p["writes_per_account"], 0.2),
          p["sms_total"])
    check("queue cap = rate*ttl/3600", close(p["queue_cap"], p["rate"] * 105 / 3600, 0.05),
          p["queue_cap"])
    p3 = P.plan(100, solvers=10, cards=10, workers=10)
    check("10 solvers + 10 cards -> writes bind", p3["binding"] == "writes", p3["per_hour"])
    check("... and the sustained wall still caps it at 25.5/h", close(p3["rate"], 25.5, 0.05),
          p3["rate"])
    p1 = P.plan(100, solvers=10, write_per_hour=300)
    check("10 solvers, 1 card, 300 writes/h -> sms binds", p1["binding"] == "sms",
          p1["per_hour"])
    p0 = P.plan(10, solvers=0)
    check("no solvers -> rate 0", p0["rate"] == 0 and p0["hours"] is None, p0["rate"])
    pcard = P.plan(10, cards=0)
    check("no cards -> sms binds at 0", pcard["binding"] == "sms" and pcard["hours"] is None,
          pcard["per_hour"])
    check("advice mentions the binding wall", p["binding"] in p["advice"], p["advice"])
    check("advice names the per-IP write budget", "30 writes/h" in p["advice"], p["advice"])

    print("[5] deadline mode")
    n = P.needs(500, 6)
    check("rate 83.3/h", close(n["accounts_per_hour"], 83.33, 0.05), n["accounts_per_hour"])
    check("3 solvers", n["solvers"] == 3, n["solvers"])
    check("2 cards", n["cards"] == 2, n["cards"])
    check("2 workers", n["workers"] == 2, n["workers"])
    check("500 in 6h is over one IP's write ceiling", n["write_cap_ok"] is False, n)
    check("... and it counts the IPs: 98/h needs 4", n["ips_needed"] == 4, n["ips_needed"])
    n_ok = P.needs(150, 6)
    check("150 in 6h fits one IP (25/h -> 29.4 writes/h)",
          n_ok["write_cap_ok"] is True and n_ok["ips_needed"] == 1, n_ok)
    n1 = P.needs(500, 1)
    check("1h is over the write ceiling", n1["write_cap_ok"] is False, n1)
    check("1h needs 20 IPs", n1["ips_needed"] == 20, n1["ips_needed"])
    check("1h needs 18 solvers", n1["solvers"] == 18, n1["solvers"])
    try:
        P.needs(10, 0)
        check("hours=0 raises", False)
    except ValueError:
        check("hours=0 raises", True)

    print("[6] render")
    lines = P.render(p)
    joined = "\n".join(lines)
    check("header names the target", "target=100" in lines[0], lines[0])
    check("one binding marker", sum(1 for l in lines if "binding" in l) == 1,
          [l for l in lines if "binding" in l])
    check("marker on the writes row", any("writes" in l and "binding" in l for l in lines),
          lines)
    check("write row names the per-IP budget", any("30 writes/h" in l for l in lines), lines)
    check("eta line printed", any(l.strip().startswith("eta") for l in lines), lines)
    check("advice line printed", any(l.strip().startswith("advice") for l in lines), lines)
    check("never-eta wording", "never" in "\n".join(P.render(p0)), P.render(p0))

    print("[6b] pool yield: already-registered numbers burn a token, not an SMS")
    w = P.writes_per_account(P.ERROR_RATE, P.MAX_FAIL)
    check("taken_rate=0 keeps the old formula", close(P.tokens_per_account(P.ERROR_RATE), 1 + w))
    check("30% taken -> 1/0.7 + writes",
          close(P.tokens_per_account(P.ERROR_RATE, P.MAX_FAIL, 0.3), 1 / 0.7 + w, 1e-6),
          P.tokens_per_account(P.ERROR_RATE, P.MAX_FAIL, 0.3))
    p_plain = P.plan(100)
    p_same = P.plan(100, taken_rate=0.0)
    check("explicit 0.0 is identical to the default",
          p_plain["rate"] == p_same["rate"] and p_plain["tokens_total"] == p_same["tokens_total"])
    p_pool = P.plan(100, taken_rate=0.3)
    check("tokens wall drops (more tokens per account)",
          p_pool["per_hour"]["tokens"] < p_plain["per_hour"]["tokens"], p_pool["per_hour"])
    check("sms/writes/workers walls unchanged",
          all(close(p_pool["per_hour"][k], p_plain["per_hour"][k]) for k in ("sms", "writes", "workers")),
          (p_plain["per_hour"], p_pool["per_hour"]))
    check("sms total unchanged (taken numbers never send)",
          close(p_pool["sms_total"], p_plain["sms_total"]), (p_plain["sms_total"], p_pool["sms_total"]))
    check("phones to buy = ceil(100/0.7) = 143", p_pool["phones_needed"] == 143, p_pool["phones_needed"])
    check("yield reported as 0.7", close(p_pool["yield"], 0.7, 1e-6), p_pool["yield"])
    check("tokens total = target * tokens/account",
          close(p_pool["tokens_total"], round(100 * (1 / 0.7 + w), 1), 0.15), p_pool["tokens_total"])
    # rate = token_rate / (1/yield + writes_per_account); the *token* wall moves,
    # it is not "old rate * yield" (that would double-count the retry writes)
    check("rate is the yield-scaled token wall",
          close(p_pool["rate"],
                round(P.token_rate_per_hour(1.0, P.PASS_RATE, P.SOLVE_SECONDS) / (1 / 0.7 + w), 2),
                0.02),
          (p_pool["rate"], P.token_rate_per_hour(1.0, P.PASS_RATE, P.SOLVE_SECONDS) / (1 / 0.7 + w)))
    lines = P.render(p_pool)
    check("pool line names the yield", any("70%" in l and "143" in l for l in lines), lines)
    check("plain plan has no pool line", not any("pool" in l for l in P.render(p_plain)),
          P.render(p_plain))

    n_pool = P.needs(100, 4, taken_rate=0.3)
    check("needs() also buys 143 numbers", n_pool["phones_needed"] == 143, n_pool)
    check("needs() tokens include the pool waste", close(n_pool["tokens_total"], 260.5, 0.6),
          n_pool["tokens_total"])
    check("needs() sms ignore taken numbers", close(n_pool["sms_total"], 117.6, 0.2),
          n_pool["sms_total"])

    print("[6c] --reset-taken: a rescued number is not waste")
    c = P.pool_cost(P.ERROR_RATE, P.MAX_FAIL, 0.5, reset_taken=True, reset_ok=1.0)
    check("yield 1.0 (every bought number delivers)", close(c["yield"], 1.0, 1e-9), c["yield"])
    check("tokens = fresh*(1+w) + 2*t",
          close(c["tokens"], 0.5 * (1 + w) + 1.0, 1e-6), c["tokens"])
    # one SMS per number (fresh or rescued) plus the retry resends of the fresh half
    check("one SMS per number, resends only on the fresh half",
          close(c["sms"], 0.5 * w + 0.5, 1e-9), c["sms"])
    check("writes only for the fresh half", close(c["writes"], 0.5 * w, 1e-9), c["writes"])
    check("skip mode cost unchanged",
          close(P.pool_cost(P.ERROR_RATE, P.MAX_FAIL, 0.5)["tokens"], 2.0 + w, 1e-6),
          P.pool_cost(P.ERROR_RATE, P.MAX_FAIL, 0.5)["tokens"])
    p_skip = P.plan(100, taken_rate=0.5)
    p_reset = P.plan(100, taken_rate=0.5, reset_taken=True)
    check("skip buys 200 numbers, reset buys 100",
          (p_skip["phones_needed"], p_reset["phones_needed"]) == (200, 100),
          (p_skip["phones_needed"], p_reset["phones_needed"]))
    check("reset spends fewer tokens per account",
          p_reset["tokens_per_account"] < p_skip["tokens_per_account"],
          (p_reset["tokens_per_account"], p_skip["tokens_per_account"]))
    check("so the token wall rises",
          p_reset["per_hour"]["tokens"] > p_skip["per_hour"]["tokens"],
          (p_reset["per_hour"]["tokens"], p_skip["per_hour"]["tokens"]))
    check("writes wall never worsens",
          p_reset["per_hour"]["writes"] >= p_skip["per_hour"]["writes"],
          (p_reset["per_hour"]["writes"], p_skip["per_hour"]["writes"]))
    check("sms per account = 0.5*w + 0.5",
          close(p_reset["sms_per_account"], 0.5 * w + 0.5, 1e-3),
          p_reset["sms_per_account"])
    lines = P.render(p_reset)
    check("pool line explains the rescue",
          any("recovered by password reset" in l for l in lines), lines)
    p_half = P.plan(100, taken_rate=0.5, reset_taken=True, reset_ok=0.5)
    check("reset_ok 0.5 -> yield 0.75", close(p_half["yield"], 0.75, 1e-6), p_half["yield"])
    check("... and 134 numbers", p_half["phones_needed"] == 134, p_half["phones_needed"])
    n_reset = P.needs(100, 4, taken_rate=0.5, reset_taken=True)
    check("needs() uses the reset cost too", n_reset["phones_needed"] == 100,
          n_reset["phones_needed"])
    check("needs() reports reset mode", n_reset.get("reset_taken") is True, n_reset.get("reset_taken"))

    print("[6d] form captcha pricing (R154): only device is measured free; reuse cannot deliver")
    check("default is fresh -> 1 token per write", P.form_tokens_per_write() == 1.0,
          P.form_tokens_per_write())
    check("fresh is a constant 1.0", P.form_tokens_per_write("fresh", 0.0) == 1.0)
    check("auto pays the retry (send-code spent the token) -> 1",
          P.form_tokens_per_write("auto") == 1.0, P.form_tokens_per_write("auto"))
    try:
        P.form_tokens_per_write("reuse")
        check("reuse is refused, not priced at 0", False)
    except ValueError as e:
        check("reuse is refused, not priced at 0", "reuse" in str(e), str(e))
    check("device with full trust -> 0", P.form_tokens_per_write("device", 1.0) == 0.0)
    check("device with half trust -> 0.5", close(P.form_tokens_per_write("device", 0.5), 0.5, 1e-9),
          P.form_tokens_per_write("device", 0.5))
    check("policy names are case/space tolerant", P.form_tokens_per_write(" DEVICE ", 1.0) == 0.0)
    check("None falls back to fresh", P.form_tokens_per_write(None) == 1.0)
    try:
        P.form_tokens_per_write("who-knows")
        check("unknown policy raises", False)
    except ValueError as e:
        check("unknown policy raises", "who-knows" in str(e), str(e))

    w2 = P.writes_per_account(0.2, 5)
    check("old default numbers unchanged", close(w2, 1.2496, 1e-4), w2)
    check("tokens/account default still 2.2496",
          close(P.tokens_per_account(0.2, 5), 2.2496, 1e-4), P.tokens_per_account(0.2, 5))
    check("auto -> same as fresh (retry always happens)",
          close(P.tokens_per_account(0.2, 5, form_captcha="auto"), 2.2496, 1e-4),
          P.tokens_per_account(0.2, 5, form_captcha="auto"))
    try:
        P.tokens_per_account(0.2, 5, form_captcha="reuse")
        check("reuse cannot be budgeted", False)
    except ValueError:
        check("reuse cannot be budgeted", True)
    check("device + full trust -> exactly the send-code token",
          close(P.tokens_per_account(0.2, 5, form_captcha="device", device_trust=1.0), 1.0, 1e-9))
    check("device + half trust -> 1 + 1.2496*0.5",
          close(P.tokens_per_account(0.2, 5, form_captcha="device", device_trust=0.5), 1.6248, 1e-4),
          P.tokens_per_account(0.2, 5, form_captcha="device", device_trust=0.5))
    check("device trust clamps at 1", close(P.tokens_per_account(0.2, 5, form_captcha="device",
                                                               device_trust=9.0), 1.0, 1e-9))

    lim_auto = P.ceilings(form_captcha="auto")
    check("auto ceiling == the fresh wall, 28.9/h", close(lim_auto["tokens"], 28.9, 0.05),
          lim_auto["tokens"])
    check("prod ceilings() default keeps the 28.9/h wall", close(P.ceilings()["tokens"], 28.9, 0.05),
          P.ceilings()["tokens"])
    p_auto = P.plan(100, form_captcha="auto")
    check("plan exposes the policy", p_auto["form_captcha"] == "auto", p_auto.get("form_captcha"))
    check("auto plan charges 1 token/write", p_auto["form_tokens_per_write"] == 1.0,
          p_auto.get("form_tokens_per_write"))
    check("auto plan buys the same 218 tokens as fresh",
          close(p_auto["tokens_total"], 217.5, 0.15), p_auto["tokens_total"])
    p_fresh = P.plan(100)
    check("fresh plan still charges 1 token/write", p_fresh["form_tokens_per_write"] == 1.0,
          p_fresh["form_tokens_per_write"])
    check("fresh plan still buys 218 tokens", close(p_fresh["tokens_total"], 217.5, 0.15),
          p_fresh["tokens_total"])
    check("render names the policy", any(l.strip().startswith("form") and "policy=fresh" in l
                                         for l in P.render(p_fresh)), P.render(p_fresh))
    check("render names auto", any("policy=auto" in l for l in P.render(p_auto)), P.render(p_auto))
    check("render admits the wasted write",
          any("wasted /register write" in l for l in P.render(p_auto)), P.render(p_auto))
    check("fresh render has no waste note",
          not any("wasted" in l for l in P.render(p_fresh)), P.render(p_fresh))

    print("[7] CLI")
    script = os.path.join(TOOLKIT, "vmax_plan.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    def run(*args):
        r = subprocess.run([sys.executable, script, *args], cwd=TOOLKIT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    rc, out = run("--target", "100")
    check("table exit 0", rc == 0, (rc, out[-200:]))
    check("table shows binding", "binding" in out, out[:200])
    rc, out = run("--target", "100", "--json")
    data = json.loads(out)
    check("json exit 0", rc == 0, rc)
    check("json has per_hour", set(data["per_hour"]) == {"tokens", "writes", "sms", "workers"},
          data.get("per_hour"))
    check("json binding writes", data["binding"] == "writes", data["binding"])
    check("json echoes the write budget", data["inputs"]["write_per_hour"] == 30.0,
          data.get("inputs"))
    rc, out = run("--target", "100", "--write-per-hour", "300", "--json")
    d_burst = json.loads(out)
    check("--write-per-hour 300 -> tokens bind", d_burst["binding"] == "tokens",
          d_burst.get("per_hour"))
    rc, out = run("--target", "100", "--write-per-hour", "0")
    check("--write-per-hour 0 -> exit 2", rc == 2, (rc, out[-120:]))
    rc, out = run("--target", "0")
    check("target 0 -> exit 2", rc == 2, (rc, out[-120:]))
    rc, out = run("--target", "10", "--deadline-hours", "0")
    check("deadline 0 -> exit 2", rc == 2, (rc, out[-120:]))
    rc, out = run("--target", "500", "--deadline-hours", "1")
    check("deadline text flags the ceiling", "OVER THE 429 CEILING" in out, out[-200:])
    rc, out = run("--target", "500", "--deadline-hours", "6", "--json")
    n2 = json.loads(out)
    check("deadline json solvers", n2["solvers"] == 3, n2.get("solvers"))
    rc, out = run("--target", "100", "--taken-rate", "0.3", "--json")
    dp = json.loads(out)
    check("cli --taken-rate exit 0", rc == 0, (rc, out[-200:]))
    check("cli json exposes phones_needed", dp["phones_needed"] == 143, dp.get("phones_needed"))
    check("cli json exposes yield", close(dp["yield"], 0.7, 1e-6), dp.get("yield"))
    rc, out = run("--target", "100", "--taken-rate", "1")
    check("taken-rate 1.0 -> exit 2", rc == 2, (rc, out[-160:]))
    rc, out = run("--target", "100", "--taken-rate", "-0.1")
    check("negative taken-rate -> exit 2", rc == 2, (rc, out[-160:]))
    rc, out = run("--target", "100", "--form-captcha", "auto", "--json")
    da = json.loads(out)
    check("cli --form-captcha auto exit 0", rc == 0, (rc, out[-200:]))
    check("cli json exposes the policy", da.get("form_captcha") == "auto", da.get("form_captcha"))
    check("cli json charges one token for the form", da.get("form_tokens_per_write") == 1.0,
          da.get("form_tokens_per_write"))
    check("cli auto buys the fresh amount", close(da.get("tokens_total", 0), 217.5, 0.15),
          da.get("tokens_total"))
    rc, out = run("--target", "100", "--form-captcha", "reuse")
    check("cli --form-captcha reuse -> exit 2 with a reason",
          rc == 2 and "cannot deliver" in out, (rc, out[-160:]))
    rc, out = run("--target", "100", "--form-captcha", "device", "--device-trust", "0.5", "--json")
    dd = json.loads(out)
    check("device 0.5 -> 0.5 token/write", close(dd.get("form_tokens_per_write", -1), 0.5, 1e-9),
          dd.get("form_tokens_per_write"))
    rc, out = run("--target", "100", "--form-captcha", "bogus")
    check("unknown --form-captcha -> exit 2", rc == 2, (rc, out[-160:]))
    rc, out = run("--target", "100", "--device-trust", "1.5")
    check("--device-trust 1.5 -> exit 2", rc == 2, (rc, out[-160:]))
    rc, out = run("--target", "100", "--device-trust", "-0.1")
    check("--device-trust -0.1 -> exit 2", rc == 2, (rc, out[-160:]))
    rc, out = run("--target", "100", "--form-captcha", "device", "--deadline-hours", "2")
    check("deadline honours the free form", rc == 0 and "tokens   100" in out, out[-200:])

    rc, out = run("--target", "20", "--solvers", "10", "--cards", "10", "--workers", "10")
    check("--solvers shifts the binding", "limited by writes" in out, out[-300:])
    rc, out = run("--target", "20", "--cards", "10", "--workers", "10",
                  "--write-per-hour", "300")
    check("... but not past the token wall when writes are cheap",
          "limited by tokens" in out, out[-300:])

    rc, out = run("--target", "100", "--taken-rate", "0.5", "--reset-taken", "--json")
    dpr = json.loads(out)
    check("cli --reset-taken exit 0", rc == 0, (rc, out[-200:]))
    check("cli json buys 100 numbers", dpr.get("phones_needed") == 100, dpr.get("phones_needed"))
    check("cli json flags the mode", dpr.get("reset_taken") is True, dpr.get("reset_taken"))
    check("cli json exposes sms_per_account",
          close(dpr.get("sms_per_account", 0), 0.5 * w + 0.5, 1e-3),
          dpr.get("sms_per_account"))
    rc, out = run("--target", "100", "--taken-rate", "0.5", "--reset-taken", "--reset-ok", "0")
    check("--reset-ok 0 -> exit 2", rc == 2, (rc, out[-160:]))
    rc, out = run("--target", "100", "--taken-rate", "0.5", "--reset-taken", "--reset-ok", "1.5")
    check("--reset-ok 1.5 -> exit 2", rc == 2, (rc, out[-160:]))

    print()
    if fails:
        print(f"FAILED {len(fails)}: " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
