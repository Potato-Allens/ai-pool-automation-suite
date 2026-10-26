# -*- coding: utf-8 -*-
"""Run every offline suite (no network, no SMS, no real captcha) and judge them.

    python tests/run_all.py                 # all suites, isolated pacer per suite
    python tests/run_all.py --only mock_e2e,login_mock
    python tests/run_all.py --list
    python tests/run_all.py --log regress.log

Why this exists
---------------
The suites were run by hand, one PowerShell loop at a time, and that loop had two
flaws that cost real debugging time:

* it did not look at exit codes or count FAIL lines, so "全绿" was a human claim;
* it leaked the toolkit's own ``.vmax_pace.json`` into every child.  That file
  carries a real ban window from live runs, so an unrelated mock suite could sit
  and sleep 45 s per write and then look flaky.

So each suite here gets its own pacer state, its own temp dir, ``VMAX_RESEND_WAIT=0``
and utf-8 stdio.  A suite counts as green only when it exits 0 **and** printed no
``FAIL `` line.

Exit codes: 0 all green, 1 something failed, 2 bad args.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time

# Redirected logs (python tests\run_all.py > log) are not a tty, so Windows
# would fall back to GBK and die on the first non-ASCII test name.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV_KEYS = ("VMAX_PACE_STATE", "VMAX_RESEND_WAIT", "PYTHONIOENCODING",
            "VMAX_NOPACE", "VMAX_POP_LOG", "VMAX_BASE")
PASS_RE = re.compile(r"(?m)^\s*PASS ")
FAIL_RE = re.compile(r"(?m)^\s*FAIL ")


def discover() -> list[str]:
    names = []
    for fn in sorted(os.listdir(HERE)):
        if fn == "mock_e2e.py" or (fn.startswith("test_") and fn.endswith(".py")):
            names.append(fn)
    # mock_e2e is the scenario suite: run it first, it is the slowest and loudest.
    names.sort(key=lambda n: (n != "mock_e2e.py", n))
    return names


def run_suite(name: str, timeout: float, tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    env["VMAX_PACE_STATE"] = os.path.join(tmp, f"pace_{name}.json")
    env["VMAX_RESEND_WAIT"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    t0 = time.time()
    try:
        cp = subprocess.run([sys.executable, "-u", os.path.join("tests", name)],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, env=env)
        out = (cp.stdout or "") + (cp.stderr or "")
        code = cp.returncode
        timed_out = False
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") if isinstance(e.stdout, str) else "") + "\n[run_all] TIMEOUT"
        code = -1
        timed_out = True
    passes = len(PASS_RE.findall(out))
    fails = len(FAIL_RE.findall(out))
    return {"name": name, "exit": code, "pass": passes, "fail": fails,
            "seconds": round(time.time() - t0, 1), "out": out, "timeout": timed_out,
            "green": code == 0 and fails == 0 and not timed_out}


def summary_line(r: dict) -> str:
    """One console line per suite.  ``vmax_docaudit.py`` parses exactly this shape."""
    return (f"{r['name']:32} {'OK  ' if r['green'] else 'FAIL'}  pass={r['pass']:<3} "
            f"fail={r['fail']:<2} exit={r['exit']:<3} {r['seconds']}s")


def build_log(head: str, items: list, elapsed: float) -> str:
    """Text of the ``--log`` file: header, per-suite block + summary, TOTAL, verdict.

    R132: the file used to carry only the raw per-suite output and a TOTAL line, so
    the ``name OK pass=N`` rows that ``vmax_docaudit.py`` reads were missing and the
    documented audit command died with "no per-suite lines".  ``items`` is a list of
    ``(block_text, result_dict)``; ``--quiet`` only silences the console, never this.
    """
    lines = [head, ""]
    for block, r in items:
        lines.append(block.rstrip("\n"))
        lines.append(summary_line(r))
        lines.append("")
    total_pass = sum(r["pass"] for _, r in items)
    total_fail = sum(r["fail"] for _, r in items)
    bad = [r["name"] for _, r in items if not r["green"]]
    lines.append(f"TOTAL suites={len(items)} pass={total_pass} fail={total_fail} "
                 f"bad={len(bad)} elapsed={elapsed}s")
    lines.append("FAILED: " + ", ".join(bad) if bad else "ALL SUITES GREEN")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="run every offline vmax suite")
    ap.add_argument("--only", default="", help="comma separated suite names (no .py)")
    ap.add_argument("--list", action="store_true", help="print the suite list and stop")
    ap.add_argument("--timeout", type=float, default=300.0, help="per-suite seconds")
    ap.add_argument("--log", default="", help="also write the full output here")
    ap.add_argument("--quiet", action="store_true", help="only the summary lines")
    args = ap.parse_args(argv)

    suites = discover()
    if args.only:
        # accept "report", "test_report" and "test_report.py" for the same suite
        def key(n: str) -> str:
            n = n.removesuffix(".py")
            return n[5:] if n.startswith("test_") else n

        want = [key(s.strip()) for s in args.only.split(",") if s.strip()]
        suites = [s for s in suites if key(s) in want]
        missing = [w for w in want if w not in [key(s) for s in suites]]
        if missing:
            print(f"[run_all] unknown suite(s): {', '.join(missing)}", flush=True)
            return 2
    if args.list:
        for s in suites:
            print(s)
        return 0
    if not suites:
        print("[run_all] nothing to run", flush=True)
        return 2

    tmp = tempfile.mkdtemp(prefix="runall_")
    items, results = [], []
    head = f"[run_all] {len(suites)} suite(s), pacer state per suite in {tmp}"
    print(head, flush=True)
    for name in suites:
        r = run_suite(name, args.timeout, tmp)
        results.append(r)
        block = (f"===== {name} (exit={r['exit']} pass={r['pass']} fail={r['fail']}) =====\n"
                 + r["out"].rstrip("\n"))
        items.append((block, r))
        if not args.quiet:
            print(r["out"].rstrip("\n"))
            print()
        print(summary_line(r), flush=True)

    total_pass = sum(r["pass"] for r in results)
    total_fail = sum(r["fail"] for r in results)
    bad = [r for r in results if not r["green"]]
    elapsed = round(sum(r["seconds"] for r in results), 1)

    if args.log:
        with open(args.log, "w", encoding="utf-8") as f:
            f.write(build_log(head, items, elapsed))

    print()
    print(f"TOTAL suites={len(results)} pass={total_pass} fail={total_fail} "
          f"bad={len(bad)} elapsed={elapsed}s")
    if bad:
        print("FAILED: " + ", ".join(r["name"] for r in bad))
        return 1
    print("ALL SUITES GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
