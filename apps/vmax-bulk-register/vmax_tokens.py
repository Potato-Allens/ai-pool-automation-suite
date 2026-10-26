# -*- coding: utf-8 -*-
"""Captcha token farm with **backpressure** -- produce only as fast as you consume.

    python vmax_tokens.py --max-queue 2 --every 12 --workers 4 --duration 1800

Why backpressure
----------------
Measured 2026-10-26 (PROTOCOL S6.6): an Aliyun token stays valid for only
**~90-120 s** (76.5 s worked, 127.7 s was rejected) and a single use consumes
it.  A deep queue is therefore worthless: solve 100 tokens up front and most of
them are dead before anyone picks them up.

So the farm now:
  * stops producing while ``--max-queue`` unconsumed tokens are waiting,
  * spaces writes globally with ``--every`` seconds (shared across workers),
  * optionally runs for ``--duration`` seconds instead of a fixed count,
  * prunes tokens older than ``--stale-after`` at startup (they cannot work).

``--max-queue`` also governs shutdown: once ``--n`` tokens have been claimed the
worker exits even while the queue is still full, so a smoke run
(``--n 1 --max-queue 1``) terminates by itself instead of waiting for a consumer.

Queue format: one JSON file per token, written atomically
(``tok_<ms>_<pid>_<seq>.json``); consumers take the queue lock and read the
payload before renaming to ``.used`` (see ``vmax_register.pop_token``).

Typical use next to a batch run::

    python vmax_tokens.py --max-queue 2 --every 12 --workers 4 --duration 3600 &
    python vmax_batch.py --input accounts.csv --captcha queue --queue-dir tokens ...
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()

from vmax_client import VmaxAuth, pkce_pair  # noqa: E402

TOKEN_GLOB_PREFIX = "tok_"


def pending(out_dir: str) -> int:
    """Number of unconsumed tokens waiting in the queue."""
    try:
        return len([f for f in os.listdir(out_dir)
                    if f.startswith(TOKEN_GLOB_PREFIX) and f.endswith(".json")])
    except OSError:
        return 0


def prune_stale(out_dir: str, max_age: float) -> int:
    """Rename tokens older than *max_age* seconds to ``.stale``; return the count."""
    if max_age <= 0:
        return 0
    n = 0
    now = time.time()
    try:
        names = os.listdir(out_dir)
    except OSError:
        return 0
    for name in names:
        if not (name.startswith(TOKEN_GLOB_PREFIX) and name.endswith(".json")):
            continue
        path = os.path.join(out_dir, name)
        try:
            if now - os.path.getmtime(path) > max_age:
                os.replace(path, path + ".stale")
                n += 1
        except OSError:
            continue
    return n


def pace_delay(last_ts: float, every: float, now: float | None = None) -> float:
    """Seconds to sleep so that two writes are at least *every* apart."""
    if every <= 0:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, (last_ts + every) - now)


def should_stop(claimed: int, target: int, queued: int, max_queue: int,
                deadline: float | None, now: float | None = None) -> str | None:
    """Decide whether a worker should stop: reason string, or None to keep going.

    Order matters.  ``target`` is checked *before* backpressure, because a farm
    started with ``--max-queue 1 --n 1`` produces its one token and then waits
    for a consumer that will never come (observed live 2026-10-26: the worker
    hung forever after a successful solve and had to be killed by hand).
    """
    now = time.time() if now is None else now
    if deadline is not None and now > deadline:
        return "deadline"
    if claimed >= target:
        return "target"
    if max_queue > 0 and queued >= max_queue:
        return "backpressure"
    return None


def fail_backoff(consecutive: int, base: float, cap: float = 300.0) -> float:
    """Seconds to wait after *consecutive* failed tokens.

    Aliyun has bad windows where every pick misses (observed 6/6 failures in a
    row).  Hammering it just burns CPU and proxy traffic, so back off linearly
    with a cap.
    """
    if consecutive <= 0 or base <= 0:
        return 0.0
    return min(base * consecutive, cap)


def _login_url(base: str, redirect: str | None, platform: str) -> str:
    _, challenge = pkce_pair()
    return VmaxAuth.login_url(challenge, state=secrets.token_urlsafe(16),
                              client=platform, base=base, redirect_uri=redirect)


def _write_token(out_dir: str, payload: dict) -> str:
    os.makedirs(out_dir, exist_ok=True)
    name = f"{TOKEN_GLOB_PREFIX}{int(time.time()*1000)}_{os.getpid()}_{payload['seq']:04d}.json"
    tmp = os.path.join(out_dir, "." + name + ".tmp")
    final = os.path.join(out_dir, name)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, final)
    return final


def _worker(idx: int, target, counter, produced, failures, out_dir, base, redirect,
            platform, proxy, attempts, headless, max_queue, every, deadline,
            pace_lock, last_write, fail_cooldown=20.0, max_consec_fail=8):
    from vmax_captcha import InpaintingSolver

    url = _login_url(base, redirect, platform)
    consec = 0
    with InpaintingSolver(headless=headless, proxy=proxy) as sv:
        while True:
            with counter.get_lock():
                claimed = counter.value
            reason = should_stop(claimed, target.value, pending(out_dir),
                                 max_queue, deadline)
            if reason in ("deadline", "target"):
                return
            # backpressure: never build a queue that will expire before use
            if reason == "backpressure":
                time.sleep(1.5)
                continue
            with counter.get_lock():
                if counter.value >= target.value:
                    return
                counter.value += 1
                seq = counter.value
            t0 = time.time()
            try:
                param = sv.solve(url, max_attempts=attempts)
            except Exception as e:  # noqa: BLE001 - keep the farm alive
                with failures.get_lock():
                    failures.value += 1
                consec += 1
                print(f"[farm w{idx} #{seq}] FAILED after {attempts}: {type(e).__name__} "
                      f"(consecutive={consec})", flush=True)
                if max_consec_fail and consec >= max_consec_fail:
                    print(f"[farm w{idx}] giving up after {consec} consecutive failures -- "
                          f"the captcha service looks unhealthy; restart the farm later", flush=True)
                    return
                d = fail_backoff(consec, fail_cooldown)
                if d > 0:
                    print(f"[farm w{idx}] cooling down {d:.0f}s", flush=True)
                    time.sleep(d)
                continue
            # global spacing across all workers
            if every > 0:
                with pace_lock:
                    d = pace_delay(last_write.value, every)
                    if d > 0:
                        time.sleep(d)
                    last_write.value = time.time()
            path = _write_token(out_dir, {
                "param": param, "ts": int(time.time()), "seq": seq, "pid": os.getpid(),
                "base": base, "platform": platform, "seconds": round(time.time() - t0, 1),
            })
            consec = 0
            with produced.get_lock():
                produced.value += 1
            print(f"[farm w{idx} #{seq}] ok {len(param)}B {round(time.time()-t0,1)}s "
                  f"queue={pending(out_dir)} -> {os.path.basename(path)}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="token farm with consumption backpressure")
    ap.add_argument("--n", type=int, default=10, help="stop after this many tokens (0 = unlimited)")
    ap.add_argument("--duration", type=float, default=0,
                    help="run for this many seconds instead of --n")
    ap.add_argument("--workers", type=int, default=2, help="parallel browser workers")
    ap.add_argument("--max-queue", type=int, default=2,
                    help="pause production while this many unconsumed tokens are waiting (0 = off)")
    ap.add_argument("--every", type=float, default=0,
                    help="global minimum seconds between two produced tokens (0 = as fast as possible)")
    ap.add_argument("--stale-after", type=float, default=120,
                    help="at startup, rename tokens older than this many seconds (measured TTL ~90-120s)")
    ap.add_argument("--out-dir", default="tokens")
    ap.add_argument("--base", default=None, help="auth base URL (env VMAX_BASE)")
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--attempts", type=int, default=6, help="contexts per token before giving up")
    ap.add_argument("--fail-cooldown", type=float, default=20.0,
                    help="seconds to sleep after a failed token, multiplied by the streak (0 = off)")
    ap.add_argument("--max-consec-fail", type=int, default=8,
                    help="exit a worker after this many consecutive failed tokens (0 = never)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    base = (args.base or os.environ.get("VMAX_BASE") or "https://auth.vobilemax.cn").rstrip("/")
    os.makedirs(args.out_dir, exist_ok=True)
    pruned = prune_stale(args.out_dir, args.stale_after)
    print(f"[farm] target={args.n or 'unlimited'} duration={args.duration or '-'}s "
          f"workers={args.workers} max_queue={args.max_queue} every={args.every}s "
          f"base={base} -> {args.out_dir}", flush=True)
    if pruned:
        print(f"[farm] pruned {pruned} token(s) older than {args.stale_after:.0f}s", flush=True)

    deadline = (time.time() + args.duration) if args.duration > 0 else None
    target = mp.Value("i", args.n if args.n > 0 else 10 ** 9)
    counter = mp.Value("i", 0)
    produced = mp.Value("i", 0)
    failures = mp.Value("i", 0)
    pace_lock = mp.Lock()
    last_write = mp.Value("d", 0.0)
    procs = [mp.Process(target=_worker, args=(
        i, target, counter, produced, failures, args.out_dir, base, args.redirect,
        args.platform, args.proxy, args.attempts, not args.headed,
        args.max_queue, args.every, deadline, pace_lock, last_write,
        args.fail_cooldown, args.max_consec_fail)) for i in range(args.workers)]
    t0 = time.time()
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    dt = time.time() - t0
    print(f"[farm] done in {dt:.0f}s: produced={produced.value} "
          f"failed={failures.value} attempts={counter.value} queue_left={pending(args.out_dir)}",
          flush=True)
    return 0 if produced.value > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
