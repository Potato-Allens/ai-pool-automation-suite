# -*- coding: utf-8 -*-
"""Offline checks for the token farm's backpressure helpers (no browser)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vmax_tokens as T  # noqa: E402
from vmax_register import pop_token  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def touch(d, name, age_s):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"param": "x", "ts": int(time.time())}, f)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def main():
    d = tempfile.mkdtemp(prefix="farm_")
    print("[1] pending() counts only live tok_*.json")
    touch(d, "tok_1.json", 5)
    touch(d, "tok_2.json", 10)
    touch(d, "tok_3.json.used", 5)
    touch(d, "tok_4.json.stale", 5)
    touch(d, "notes.txt", 5)
    check("two live tokens", T.pending(d) == 2, T.pending(d))
    check("missing dir -> 0", T.pending(os.path.join(d, "nope")) == 0)

    print("[2] prune_stale() retires expired tokens")
    touch(d, "tok_old.json", 300)
    before = T.pending(d)
    n = T.prune_stale(d, 120)
    check("pruned one", n == 1, n)
    check("queue shrank", T.pending(d) == before - 1, T.pending(d))
    check(".stale renamed", os.path.exists(os.path.join(d, "tok_old.json.stale")))
    check("fresh ones survive", os.path.exists(os.path.join(d, "tok_1.json")))
    check("prune with max_age=0 is a no-op", T.prune_stale(d, 0) == 0)

    print("[3] pace_delay() maths")
    now = 1000.0
    check("no spacing -> 0", T.pace_delay(990.0, 0, now) == 0.0)
    check("halfway -> wait half", abs(T.pace_delay(994.0, 12, now) - 6.0) < 1e-6,
          T.pace_delay(994.0, 12, now))
    check("already past -> 0", T.pace_delay(980.0, 12, now) == 0.0)
    check("exact edge -> 0", T.pace_delay(988.0, 12, now) == 0.0)

    print("[4] backpressure threshold semantics")
    # max_queue=2 means: with 2 waiting, production pauses; with 1 it may proceed.
    check("at capacity", T.pending(d) >= 2, T.pending(d))
    os.replace(os.path.join(d, "tok_1.json"), os.path.join(d, "tok_1.json.used"))
    check("after one consume, below capacity", T.pending(d) < 2, T.pending(d))

    print("[5] should_stop() ordering (regression: --n 1 --max-queue 1 hang)")
    now = 5000.0
    # the bug: one token claimed, queue full, no consumer -> must exit on target
    check("target beats backpressure",
          T.should_stop(1, 1, 1, 1, None, now) == "target",
          T.should_stop(1, 1, 1, 1, None, now))
    check("target reached, empty queue", T.should_stop(3, 3, 0, 2, None, now) == "target")
    check("deep target is fine", T.should_stop(4, 10, 0, 2, None, now) is None)
    check("full queue below target",
          T.should_stop(0, 10, 2, 2, None, now) == "backpressure",
          T.should_stop(0, 10, 2, 2, None, now))
    check("one waiting, capacity two", T.should_stop(0, 10, 1, 2, None, now) is None)
    check("max_queue=0 disables backpressure", T.should_stop(0, 10, 99, 0, None, now) is None)
    check("deadline wins", T.should_stop(0, 10, 0, 2, now - 1, now) == "deadline")
    check("future deadline is fine", T.should_stop(0, 10, 0, 2, now + 60, now) is None)
    check("unlimited target never trips", T.should_stop(999, 10 ** 9, 0, 2, None, now) is None)
    check("claimed past target also stops", T.should_stop(5, 3, 0, 2, None, now) == "target")

    print("[6] fail_backoff() keeps a bad captcha window from spinning")
    check("no streak -> no wait", T.fail_backoff(0, 20) == 0.0)
    check("one failure -> base", T.fail_backoff(1, 20) == 20.0, T.fail_backoff(1, 20))
    check("linear growth", T.fail_backoff(3, 20) == 60.0, T.fail_backoff(3, 20))
    check("capped at 300s", T.fail_backoff(100, 20) == 300.0, T.fail_backoff(100, 20))
    check("cap can be lowered", T.fail_backoff(9, 20, cap=90) == 90.0, T.fail_backoff(9, 20, cap=90))
    check("cooldown 0 disables it", T.fail_backoff(5, 0) == 0.0)

    print("[7] pop_token() never hands one token to two workers (Windows race)")
    q = tempfile.mkdtemp(prefix="pop_")
    params = []
    for i in range(6):
        path = os.path.join(q, f"tok_{i:04d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"param": f"P{i}"}, f)
        os.utime(path, (time.time() + i, time.time() + i))
        params.append(f"P{i}")
    got, errs = [], []
    lock = threading.Lock()

    def pop_one():
        try:
            v = pop_token(q, 0)
        except Exception as e:  # noqa: BLE001
            with lock:
                errs.append(f"{type(e).__name__}: {e}")
            return
        with lock:
            got.append(v)

    threads = [threading.Thread(target=pop_one) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("six claims for six tokens", len(got) == 6, got)
    check("no duplicate delivery", len(set(got)) == len(got), got)
    check("every token handed out once", set(got) == set(params), sorted(got))
    check("losers fail cleanly", len(errs) == 4 and all(e.startswith("RuntimeError") for e in errs),
          errs)
    check("no PermissionError on read", not any("Permission" in e or "FileNotFound" in e for e in errs),
          errs)
    left = [n for n in os.listdir(q) if n.startswith("tok_") and n.endswith(".json")]
    used = [n for n in os.listdir(q) if n.endswith(".used")]
    check("queue drained", not left, left)
    check("six .used markers", len(used) == 6, used)

    print("[8] pop_token() serves the oldest token first")
    q2 = tempfile.mkdtemp(prefix="pop_order_")
    for name, age in (("tok_a.json", 30), ("tok_b.json", 20), ("tok_c.json", 10)):
        pth = os.path.join(q2, name)
        with open(pth, "w", encoding="utf-8") as f:
            json.dump({"param": name[4]}, f)
        t = time.time() - age
        os.utime(pth, (t, t))
    order = [pop_token(q2, 0), pop_token(q2, 0), pop_token(q2, 0)]
    check("oldest first", order == ["a", "b", "c"], order)

    print("[9] pop_token() blocks for the farm instead of failing the account")
    q3 = tempfile.mkdtemp(prefix="pop_wait_")
    t6 = time.time()
    try:
        pop_token(q3, 0.6)
        check("empty queue raises", False)
    except RuntimeError as e:
        check("empty queue raises cleanly", "queue empty" in str(e), str(e))
    waited_empty = time.time() - t6
    check("honoured the wait budget", 0.5 <= waited_empty <= 3.0, waited_empty)

    def late_producer():
        time.sleep(1.0)
        path = os.path.join(q3, "tok_late.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"param": "LATE"}, f)

    threading.Thread(target=late_producer, daemon=True).start()
    t7 = time.time()
    got_late = pop_token(q3, 10)
    waited_late = time.time() - t7
    check("picked up the late token", got_late == "LATE", got_late)
    check("waited for the producer", 0.7 <= waited_late <= 4.0, waited_late)

    print("ALL FARM TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
