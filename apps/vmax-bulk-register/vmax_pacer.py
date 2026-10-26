# -*- coding: utf-8 -*-
"""Persistent, cross-process pacer for the auth service write endpoints.

Why this exists (measured 2026-10-26, see docs/PROTOCOL.md S7.2)
---------------------------------------------------------------
``POST /register`` and ``POST /api/auth/register`` share one IP-scoped bucket.
A burst of 7 requests in ~3 s on a fresh bucket is fine, the 8th trips it: every
later request gets

    429 {"detail":"操作过于频繁，请稍后重试","message":"..."}

with no ``Retry-After`` / ``X-RateLimit-*`` headers, and the ban lasted
>= 200 s.  Crucially, 429 responses appear to *count* as well, so polling
during a ban keeps renewing it.  GET endpoints, ``POST /api/auth/login`` and
both send-code endpoints are NOT in this bucket.

Therefore a batch runner must
  * keep a slow, jittered request rate,
  * share the ban state across worker processes, and
  * sleep through the ban instead of probing it.

State lives in a JSON file so separate processes (``vmax_batch --workers N``)
and separate runs agree on the current ban.
"""
from __future__ import annotations

import json
import os
import random
import time

DEFAULT_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vmax_pace.json")

# Measured 2026-10-26 (R75 + R154):
#   * a 7-post burst on a fresh bucket is fine, the 8th answers 429;
#   * one IP spent 11 writes over ~18 min and the 12th was refused -- a "5 per 60 s"
#     window would have allowed that, so the sustained budget is far smaller than
#     the burst;
#   * after a trip the ban outlived **27 min of silence** once it had been probed a
#     few times (every 429 during a ban escalates it).
# The defaults below therefore pace at 30 writes/h (5 per 10 min) per IP -- below
# the measured 37/h that tripped, and far below the old 300/h.
DEFAULT_CAPACITY = 5
DEFAULT_WINDOW = 600.0
DEFAULT_BAN = 600.0      # first ban: still 429 at 426 s -- do not probe, just wait
MAX_BAN = 7200.0


class Pacer:
    def __init__(self, state_path: str | None = None, capacity: int = DEFAULT_CAPACITY,
                 window: float = DEFAULT_WINDOW, base_ban: float = DEFAULT_BAN,
                 max_ban: float = MAX_BAN, enabled: bool = True, quiet: bool = False):
        self.path = state_path or os.environ.get("VMAX_PACE_STATE") or DEFAULT_STATE
        self.capacity = max(1, int(capacity))
        self.window = float(window)
        self.base_ban = float(base_ban)
        self.max_ban = float(max_ban)
        self.enabled = enabled
        self.quiet = quiet

    # ---------------- state io ----------------
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("keys"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"keys": {}}

    def _save(self, data: dict) -> None:
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _lock(self):
        return _FileLock(self.path + ".lock")

    def _entry(self, data: dict, key: str) -> dict:
        e = data["keys"].setdefault(key, {})
        e.setdefault("times", [])
        e.setdefault("ban_until", 0.0)
        e.setdefault("strikes", 0)
        e.setdefault("last_429", 0.0)
        return e

    # ---------------- api ----------------
    def acquire(self, key: str = "register") -> float:
        """Block until it is safe to send one register-write request.

        Returns the number of seconds slept. Never sends a probe request while
        a ban is active -- it just sleeps until ``ban_until``.
        """
        if not self.enabled:
            return 0.0
        slept = 0.0
        while True:
            with self._lock():
                data = self._load()
                e = self._entry(data, key)
                now = time.time()
                wait = 0.0
                if e["ban_until"] > now:
                    wait = e["ban_until"] - now
                else:
                    e["times"] = [t for t in e["times"] if now - t < self.window]
                    if len(e["times"]) >= self.capacity:
                        wait = self.window - (now - e["times"][0]) + random.uniform(0.5, 2.5)
                    else:
                        e["times"].append(now)
                        self._save(data)
                        return slept
            if wait > 0:
                wait = min(max(wait, 0.2), 1800.0) + random.uniform(0.2, 1.2)
                if not self.quiet:
                    print(f"[pace] {key}: sleeping {wait:.0f}s", flush=True)
                time.sleep(wait)
                slept += wait
            else:
                time.sleep(0.05)

    def wait_if_banned(self, key: str = "register", max_wait: float = 1800.0) -> float:
        """Sleep out an active ban *without* sending a request. Returns seconds slept.

        Call this before spending a real SMS: send-code is not in the rate-limit
        bucket, so a send during a 429 ban "succeeds", the code arrives, and then
        ``/register`` fails until the ban clears -- a wasted SIM message plus a
        burnt captcha token. Waiting costs nothing.
        """
        if not self.enabled:
            return 0.0
        slept = 0.0
        while True:
            with self._lock():
                data = self._load()
                e = self._entry(data, key)
                now = time.time()
                wait = (e["ban_until"] - now) if e["ban_until"] > now else 0.0
            if wait <= 0:
                return slept
            wait = min(wait, max_wait) + random.uniform(0.2, 1.0)
            if not self.quiet:
                print(f"[pace] {key}: banned, waiting {wait:.0f}s before spending an SMS",
                      flush=True)
            time.sleep(wait)
            slept += wait

    def on_response(self, status: int, key: str = "register", body: str = "") -> None:
        """Record the outcome. A 429 starts/extends a ban; a success cools down."""
        if not self.enabled:
            return
        with self._lock():
            data = self._load()
            e = self._entry(data, key)
            now = time.time()
            if status == 429:
                e["strikes"] = int(e["strikes"]) + 1
                ban = min(self.base_ban * (2 ** (e["strikes"] - 1)), self.max_ban)
                e["ban_until"] = now + ban
                e["last_429"] = now
                e["times"] = []
                self._save(data)
                if not self.quiet:
                    print(f"[pace] {key}: 429 -> ban {ban:.0f}s (strike {e['strikes']})", flush=True)
            else:
                if e.get("strikes") and now >= e["ban_until"] + 60:
                    e["strikes"] = max(0, int(e["strikes"]) - 1)
                self._save(data)

    def snapshot(self, key: str = "register") -> dict:
        data = self._load()
        e = self._entry(data, key)
        now = time.time()
        return {
            "state_path": self.path,
            "key": key,
            "banned": e["ban_until"] > now,
            "ban_remaining_s": max(0.0, round(e["ban_until"] - now, 1)),
            "strikes": int(e["strikes"]),
            "window_used": len([t for t in e["times"] if now - t < self.window]),
            "capacity": self.capacity,
            "window_s": self.window,
            "last_429": e["last_429"] or None,
        }

    def reset(self, key: str | None = None) -> None:
        with self._lock():
            data = self._load()
            if key:
                data["keys"].pop(key, None)
            else:
                data["keys"] = {}
            self._save(data)


class _FileLock:
    """Tiny cross-process lock (O_CREAT|O_EXCL) with stale-lock recovery."""

    def __init__(self, path: str, timeout: float = 30.0, stale: float = 120.0):
        self.path = path
        self.timeout = timeout
        self.stale = stale
        self.fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.unlink(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    return self  # degrade: proceed unlocked rather than hang
                time.sleep(0.02 + random.random() * 0.03)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return False


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="inspect / reset the vobilemax register pacer state")
    ap.add_argument("action", nargs="?", default="status", choices=["status", "reset"])
    ap.add_argument("--key", default=None, help="bucket key (default: all for reset, register for status)")
    ap.add_argument("--state", default=None)
    args = ap.parse_args(argv)
    p = Pacer(state_path=args.state)
    if args.action == "reset":
        p.reset(args.key)
        print(f"reset {args.key or 'all'} in {p.path}")
        return 0
    print(json.dumps(p.snapshot(args.key or "register"), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
