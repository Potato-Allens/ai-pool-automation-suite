# -*- coding: utf-8 -*-
"""Measure the automatic INPAINTING solver pass rate, one fresh context per try.

    python vmax_bench.py --n 12
    python vmax_bench.py --n 24 --base https://test-auth.vobilemax.cn \
        --redirect https://test-dreammaker.vobilemax.cn/auth/callback

Each round is an independent captcha challenge, so the result estimates the
single-try probability p used by `--captcha-attempts` (1-(1-p)^n).
Only the captcha is exercised: no SMS is sent, no account is created.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vmax_captcha import InpaintingSolver  # noqa: E402
from vmax_client import VmaxAuth, pkce_pair  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="INPAINTING solver pass-rate bench")
    ap.add_argument("--n", type=int, default=12, help="independent rounds")
    ap.add_argument("--base", default=None, help="auth base URL (env VMAX_BASE)")
    ap.add_argument("--redirect", default=None, help="redirect_uri override")
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--out", default=None, help="write per-round JSON here")
    args = ap.parse_args()

    base = (args.base or os.environ.get("VMAX_BASE") or "https://auth.vobilemax.cn").rstrip("/")
    _, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    url = VmaxAuth.login_url(challenge, state=state, client=args.platform,
                             base=base, redirect_uri=args.redirect)
    print(f"[bench] {args.n} rounds against {base}", flush=True)

    rounds: list[dict] = []
    ok = 0
    t0 = time.time()
    with InpaintingSolver(headless=True, proxy=args.proxy) as sv:
        for i in range(1, args.n + 1):
            r0 = time.time()
            passed = True
            try:
                sv.solve(url, max_attempts=1)
            except RuntimeError:
                passed = False
            ok += passed
            rec = {"round": i, "ok": passed, "seconds": round(time.time() - r0, 1)}
            rounds.append(rec)
            print(f"[bench] {i}/{args.n} ok={passed} ({rec['seconds']}s) "
                  f"running={ok}/{i}", flush=True)

    dt = time.time() - t0
    rate = ok / args.n if args.n else 0.0
    print(f"[bench] {ok}/{args.n} = {rate:.1%} in {dt:.0f}s "
          f"(3 tries {1 - (1 - rate) ** 3:.0%}, 6 tries {1 - (1 - rate) ** 6:.0%})", flush=True)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"base": base, "n": args.n, "ok": ok, "rate": rate,
                       "rounds": rounds}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())