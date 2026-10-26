# -*- coding: utf-8 -*-
"""Offline checks for vmax_pacer (no network, < 10 s)."""
from __future__ import annotations

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from vmax_pacer import Pacer  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="pace_")
    st = os.path.join(tmp, "pace.json")
    p = Pacer(state_path=st, capacity=2, window=0.6, base_ban=1.0, max_ban=4.0, quiet=True)

    print("[1] token bucket")
    t0 = time.time()
    p.acquire("k"); p.acquire("k")
    fast = time.time() - t0
    t1 = time.time()
    p.acquire("k")                      # third must wait for the window
    waited = time.time() - t1
    check("first two are fast", fast < 0.3, fast)
    check("third waits for window", waited >= 0.4, waited)

    print("[2] 429 starts a ban and acquire sleeps instead of probing")
    p.on_response(429, "k")
    snap = p.snapshot("k")
    check("banned", snap["banned"], snap)
    check("strike 1", snap["strikes"] == 1, snap)
    check("ban ~1s", 0.5 <= snap["ban_remaining_s"] <= 1.2, snap)
    t2 = time.time()
    p.acquire("k")
    slept = time.time() - t2
    check("acquire slept through ban", slept >= 0.8, slept)
    check("ban cleared after sleep", not p.snapshot("k")["banned"])

    print("[3] escalation")
    p.on_response(429, "k")             # strike 2 -> 2s
    check("strike 2 -> 2s", 1.0 <= p.snapshot("k")["ban_remaining_s"] <= 2.2, p.snapshot("k"))
    p.on_response(429, "k")             # strike 3 -> 4s (capped)
    check("strike 3 -> capped 4s", p.snapshot("k")["ban_remaining_s"] > 3.0, p.snapshot("k"))

    print("[4] state is shared through the file")
    p2 = Pacer(state_path=st, quiet=True)
    check("second instance sees the ban", p2.snapshot("k")["banned"], p2.snapshot("k"))

    print("[5] reset")
    p2.reset("k")
    check("reset clears ban", not p.snapshot("k")["banned"])

    print("[6] disabled pacer never blocks")
    p3 = Pacer(state_path=st, capacity=1, window=60, enabled=False, quiet=True)
    p3.on_response(429, "z")
    t3 = time.time(); p3.acquire("z")
    check("disabled is instant", time.time() - t3 < 0.2)

    print("[7] wait_if_banned() sleeps out a ban without spending a request")
    st2 = os.path.join(tmp, "pace2.json")
    q = Pacer(state_path=st2, capacity=5, window=60, base_ban=1.0, max_ban=2.0, quiet=True)
    check("clean bucket -> no wait", q.wait_if_banned("k") == 0.0)
    q.on_response(429, "k")
    used_before = q.snapshot("k")["window_used"]
    t4 = time.time()
    slept2 = q.wait_if_banned("k")
    real = time.time() - t4
    check("slept through the ban", 0.8 <= real <= 3.0, real)
    check("reports the slept time", 0.8 <= slept2 <= 3.2, slept2)
    check("ban cleared", not q.snapshot("k")["banned"])
    check("no slot consumed by waiting", q.snapshot("k")["window_used"] == used_before,
          (used_before, q.snapshot("k")["window_used"]))
    check("second call is free", q.wait_if_banned("k") == 0.0)
    qd = Pacer(state_path=st2, base_ban=60, enabled=False, quiet=True)
    qd.on_response(429, "k")
    t5 = time.time(); qd.wait_if_banned("k")
    check("disabled never waits", time.time() - t5 < 0.2)

    print("ALL PACER TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
