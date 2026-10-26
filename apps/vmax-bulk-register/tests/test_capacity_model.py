# -*- coding: utf-8 -*-
"""Cross-tool consistency of the /register write budget (R156/R157).

R156 measured that the 429 bucket is keyed on the egress IP and sustains
~30 writes/h -- not the 300/h the old "5 per 60 s" implied.  That number now
lives in three places that must agree:

    vmax_plan.py   WRITE_PER_HOUR          (capacity maths + docs)
    vmax_pacer.py  DEFAULT_CAPACITY/WINDOW (the thing that actually sleeps)
    vmax_go.py     WRITES_PER_HOUR         (the ETA printed before a run)

If one of them drifts, the run either lies about its ETA (go), oversells the
fleet (plan) or walks into a 429 (pacer).  Nothing else in the 34 suites
compares them, which is exactly how the 10x error survived 80 rounds.

Pure arithmetic: no network, no SMS, no tokens.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vmax_go as G  # noqa: E402
import vmax_pacer as PACE  # noqa: E402
import vmax_plan as P  # noqa: E402

DOCS = os.path.dirname(ROOT)
fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def close(a, b, tol=0.01):
    return abs(a - b) <= tol


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8-sig")
    except OSError:
        return None


def main():
    print("[1] one number, three places")
    check("plan says 30 writes/h", close(P.WRITE_PER_HOUR, 30.0, 1e-9), P.WRITE_PER_HOUR)
    pacer_per_hour = PACE.DEFAULT_CAPACITY * 3600.0 / PACE.DEFAULT_WINDOW
    check("pacer default is the same 30 writes/h",
          close(pacer_per_hour, P.WRITE_PER_HOUR, 1e-6), pacer_per_hour)
    check("go's ETA uses the same budget", close(G.WRITES_PER_HOUR, P.WRITE_PER_HOUR, 1e-9),
          G.WRITES_PER_HOUR)
    check("go's writes/account is the planner's",
          close(G.WRITES_PER_ACCOUNT, P.writes_per_account(P.ERROR_RATE), 0.01),
          (G.WRITES_PER_ACCOUNT, P.writes_per_account(P.ERROR_RATE)))
    burst = P.WRITE_CAP * 3600.0 / P.WRITE_WINDOW
    check("the burst wall stays well above the sustained one (5/60 s = 300/h)",
          close(burst, 300.0, 1e-6) and burst >= 5 * P.WRITE_PER_HOUR, burst)
    check("pacer waits out a real ban before retrying",
          PACE.DEFAULT_BAN >= 600.0 and PACE.MAX_BAN >= 600.0, (PACE.DEFAULT_BAN, PACE.MAX_BAN))

    print("[2] one IP: the numbers the docs quote")
    base = P.ceilings()
    check("writes wall 25.5 accounts/h", close(base["writes"], 25.5, 0.05), base["writes"])
    p = P.plan(100)
    check("writes is the binding wall", p["binding"] == "writes", p["binding"])
    check("eta 3.92 h for 100", close(p["hours"], 3.92, 0.02), p["hours"])
    rows = [{"phone": "1380000%04d" % i} for i in range(100)]
    g1 = G.plan(rows, None)
    check("go: one lane", g1["lanes"] == 1, g1["lanes"])
    check("go: ~25 accounts/h", close(g1["accounts_per_hour"], 25.4, 0.2),
          g1["accounts_per_hour"])
    check("go's ETA matches the planner's (within 6 min on 100 numbers)",
          abs(g1["eta_s"] / 3600.0 - p["hours"]) <= 0.1,
          (g1["eta_s"] / 3600.0, p["hours"]))
    check("go still says a single IP cannot be sped up by workers",
          "writes/h" in g1["rate_per_lane"], g1["rate_per_lane"])

    print("[3] fan-out: only the write wall scales")
    two = P.ceilings(ips=2)
    check("2 IPs -> writes 51.0/h", close(two["writes"], 51.0, 0.05), two["writes"])
    check("tokens/sms/workers untouched by IPs",
          all(close(two[k], base[k], 1e-9) for k in ("tokens", "sms", "workers")),
          (base, two))
    p2 = P.plan(100, ips=2)
    check("2 IPs -> tokens take over as the wall", p2["binding"] == "tokens", p2["binding"])
    check("... but never above one token wall", close(p2["rate"], base["tokens"], 0.05),
          (p2["rate"], base["tokens"]))
    check("an 8-IP fleet still stops at the token wall",
          P.plan(100, ips=8)["per_hour"]["writes"] > base["tokens"]
          and P.plan(100, ips=8)["binding"] in ("tokens", "sms", "workers"),
          P.plan(100, ips=8)["per_hour"])
    check("0 IPs deliver nothing",
          P.ceilings(ips=0)["writes"] == 0.0 and P.plan(10, ips=0)["hours"] is None,
          P.ceilings(ips=0)["writes"])
    rows2 = [{"phone": "1380001%04d" % i,
              "proxy": "http://127.0.0.1:%d" % (9000 + i % 2)} for i in range(100)]
    g2 = G.plan(rows2, None)
    check("go counts distinct proxies as lanes", g2["lanes"] == 2, g2["lanes"])
    check("go: 2 lanes -> 2x the budget", close(g2["accounts_per_hour"], 2 * 25.4, 0.3),
          g2["accounts_per_hour"])
    check("go: 2 lanes halve the ETA", close(g2["eta_s"], g1["eta_s"] / 2, 2),
          (g2["eta_s"], g1["eta_s"]))
    mixed = G.plan([{"phone": "1", "proxy": "http://a:1"}, {"phone": "2"}], "http://b:2")
    check("a row proxy replaces the default for that row only",
          mixed["lanes"] == 2 and mixed["proxies"] == ["http://a:1", "http://b:2"], mixed)

    print("[4] deadline mode: per-IP need vs fleet size")
    n = P.needs(500, 6)
    check("500 in 6h needs 4 IPs", n["ips_needed"] == 4, n["ips_needed"])
    check("... and is over the ceiling with one", n["write_cap_ok"] is False, n)
    check("... is fine with a 4-IP fleet",
          P.needs(500, 6, ips=4)["write_cap_ok"] is True, P.needs(500, 6, ips=4))
    check("... 2 IPs are still short", P.needs(500, 6, ips=2)["write_cap_ok"] is False)
    check("the per-IP cap is reported next to the fleet cap",
          close(n["write_cap_per_ip"], 30.0, 1e-9) and close(n["write_cap_per_hour"], 30.0, 1e-9),
          (n["write_cap_per_ip"], n["write_cap_per_hour"]))

    print("[5] the stale model is gone")
    src = read(os.path.join(ROOT, "vmax_go.py")) or ""
    check("no RATE_PER_WINDOW left in vmax_go.py", "RATE_PER_WINDOW" not in src)
    check("no '5 register writes / 60 s' claim left",
          not re.search(r"5\s+register\s+writes\s*/\s*60", src))
    plan_src = read(os.path.join(ROOT, "vmax_plan.py")) or ""
    check("plan keeps the burst knob and the sustained knob apart",
          "WRITE_CAP" in plan_src and "WRITE_PER_HOUR" in plan_src)

    print("[6] the docs quote the same number")
    runbook = read(os.path.join(DOCS, "docs", "RUNBOOK.md"))
    proto = read(os.path.join(DOCS, "docs", "PROTOCOL.md"))
    readme = read(os.path.join(DOCS, "README.md"))
    if runbook is None or proto is None or readme is None:
        print("  SKIP docs (clean-room copy ships only *.py)")
    else:
        check("RUNBOOK says 30 writes/h", "30 写/h" in runbook)
        check("PROTOCOL says 30 writes/h per IP", "30 写/h/IP" in proto)
        check("README quotes the 25.5 wall", "25.5" in readme)
        check("no doc still sells 255 accounts/h",
              not any("255 账号/h" in d for d in (runbook, proto, readme)))

    print()
    if fails:
        print(f"FAILED {len(fails)}: " + ", ".join(fails))
        return 1
    print("ALL CAPACITY-MODEL TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())