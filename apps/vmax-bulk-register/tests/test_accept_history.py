# -*- coding: utf-8 -*-
"""Offline checks for the accept-history view (no network, no real reports)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLKIT = os.path.dirname(HERE)
sys.path.insert(0, TOOLKIT)

import vmax_accept as A  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def mk_report(ts, checks, ready=None, base="https://auth.vobilemax.cn"):
    hard = [c["check"] for c in checks if c.get("hard", True) and not c["ok"]]
    return {"ts": ts, "base": base, "ready": (not hard) if ready is None else ready,
            "hard_failures": hard, "checks": checks}


def ok(name, hard=True):
    return {"check": name, "ok": True, "detail": "fine", "hard": hard}


def bad(name, hard=True):
    return {"check": name, "ok": False, "detail": "boom", "hard": hard}


def write(d, name, data):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def main():
    tmp = tempfile.mkdtemp(prefix="vmxhist_")
    try:
        print("[1] empty glob")
        ents, skipped = A.collect_history(os.path.join(tmp, "nope-*.json"))
        check("no entries", ents == [], ents)
        check("no skipped", skipped == [], skipped)
        lines = A.render_history(ents, skipped=skipped)
        check("says no match", any("no accept reports matched" in l for l in lines), lines)
        rc = A.print_history(os.path.join(tmp, "nope-*.json"))
        check("print_history rc=2 when nothing found", rc == 2, rc)

        print("[2] bad + non-accept files are skipped, not fatal")
        write(tmp, "accept-bad.json", None)
        with open(os.path.join(tmp, "accept-bad.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        write(tmp, "accept-other.json", {"hello": "world"})
        write(tmp, "accept-r1.json", mk_report(1000, [ok("preflight"), bad("smsd")]))
        ents, skipped = A.collect_history(os.path.join(tmp, "accept-*.json"))
        check("one usable entry", len(ents) == 1, len(ents))
        check("two skipped", len(skipped) == 2, skipped)
        check("skip reason names the file", any("accept-bad.json" in s for s in skipped), skipped)
        check("non-accept reason", any("not an accept report" in s for s in skipped), skipped)
        lines = A.render_history(ents, skipped=skipped)
        check("header counts runs", lines[0].startswith("ACCEPT HISTORY  1 run(s)"), lines[0])
        check("skipped line printed", any(l.strip().startswith("skipped") for l in lines), lines[:3])
        for name in ("accept-bad.json", "accept-other.json", "accept-r1.json"):
            os.remove(os.path.join(tmp, name))

        print("[3] ordering follows ts, not filename")
        # file names deliberately reversed vs ts order
        write(tmp, "accept-z.json", mk_report(1000, [bad("preflight")]))
        write(tmp, "accept-a.json", mk_report(3000, [ok("preflight")]))
        write(tmp, "accept-m.json", mk_report(2000, [bad("smsd")]))
        ents, _ = A.collect_history(os.path.join(tmp, "accept-*.json"))
        order = [os.path.basename(e["path"]) for e in ents]
        check("ts order", order == ["accept-z.json", "accept-m.json", "accept-a.json"], order)

        print("[4] limit keeps the newest N")
        ents, _ = A.collect_history(os.path.join(tmp, "accept-*.json"), limit=2)
        names = [os.path.basename(e["path"]) for e in ents]
        check("newest two", names == ["accept-m.json", "accept-a.json"], names)
        ents, _ = A.collect_history(os.path.join(tmp, "accept-*.json"), limit=0)
        check("limit 0 = all", len(ents) == 3, len(ents))

        print("[5] delta column (+new / -fixed / = / .)")
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        write(tmp, "accept-1.json", mk_report(1000, [bad("preflight"), bad("smsd"), ok("clock")]))
        write(tmp, "accept-2.json", mk_report(2000, [ok("preflight"), ok("smsd"), ok("clock")]))
        write(tmp, "accept-3.json", mk_report(3000, [ok("preflight"), bad("token_queue"),
                                                     ok("clock", hard=False)]))
        ents, _ = A.collect_history(os.path.join(tmp, "accept-*.json"))
        lines = A.render_history(ents, now=3100)
        body = [l for l in lines if l.strip().startswith(("01-01", "12-31", "??"))]
        first = [l for l in lines if "accept-1.json" in l][0]
        second = [l for l in lines if "accept-2.json" in l][0]
        third = [l for l in lines if "accept-3.json" in l][0]
        check("baseline run has no delta", first.rstrip().endswith("."), first)
        check("run 2 fixed 2", second.split()[-1] == "-2", second)
        check("run 3 one new failure", third.split()[-1] == "+1", third)

        print("[6] status counts: hard fail vs soft warn")
        check("run1 fail count is 2", first.split()[4] == "2", first)
        check("run1 warn count is 0", first.split()[5] == "0", first)
        counts, fails_set = A._report_stats(
            mk_report(1, [bad("opt", hard=False), ok("a"), bad("b")]))
        check("soft miss counts as warn", counts["warn"] == 1 and counts["fail"] == 1, counts)
        check("soft miss is not a hard failure", fails_set == {"b"}, fails_set)
        check("soft ok counts as skip", A._report_stats(
            mk_report(1, [ok("opt", hard=False)]))[0]["skip"] == 1)
        check("run2 clean counts", A._report_stats(ents[1]["report"])[0]["ok"] == 3,
              A._report_stats(ents[1]["report"])[0])

        print("[7] ready column + verdict flip")
        check("run1 NO", first.split()[3] == "NO", first.split()[:4])
        check("run2 yes", second.split()[3] == "yes", second.split()[:4])
        flips = [l for l in lines if l.strip().startswith("changed")]
        check("one flip line (latest)", len(flips) == 1, flips)
        check("latest flip is run 3", "accept-3.json" in flips[0], flips)
        check("latest flip says NOT ready", "verdict -> NOT ready" in flips[0], flips)
        lines_early = A.render_history(ents[:2], now=2500)
        flip_early = [l for l in lines_early if l.strip().startswith("changed")]
        check("first two runs flip to ready",
              len(flip_early) == 1 and "verdict -> ready" in flip_early[0]
              and "accept-2.json" in flip_early[0], flip_early)
        check("ready tally 1/3", any("ready    1/3" in l for l in lines),
              [l for l in lines if "ready  " in l])

        print("[8] check changes list")
        text = "\n".join(lines)
        check("preflight fail -> ok", "preflight: fail -> ok" in text, text[-600:])
        check("smsd fail -> ok", "smsd: fail -> ok" in text, text[-600:])
        check("token_queue ok -> fail", "token_queue: ok -> fail" in text, text[-600:])
        check("latest line has age", any(l.strip().startswith("latest") and "(age" in l
                                         for l in lines), lines)

        print("[9] stable verdict message")
        ents2, _ = A.collect_history(os.path.join(tmp, "accept-2.json"))
        lines2 = A.render_history(ents2, now=2000)
        check("single run is stable", any("verdict stable" in l for l in lines2), lines2)
        check("single run no delta", lines2[3].rstrip().endswith("."), lines2[3])

        print("[10] CLI wiring")
        script = os.path.join(TOOLKIT, "vmax_accept.py")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")

        def run(*args):
            r = subprocess.run([sys.executable, script, *args], cwd=tmp, env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            return r.returncode, (r.stdout or "") + (r.stderr or "")

        rc, out = run("--history-only", "accept-*.json")
        check("history-only exit 0", rc == 0, (rc, out[-300:]))
        check("history-only shows 3 runs", "3 run(s)" in out, out[:200])
        check("history-only runs no checks", "ACCEPT REPORT" not in out, out[:200])
        rc, out = run("--history-only", "zzz-*.json")
        check("history-only exit 2 when empty", rc == 2, (rc, out[-200:]))
        rc, out = run("--report", "missing.json")
        check("report missing still exit 2", rc == 2, (rc, out[-200:]))
        rc, out = run("--report", "accept-2.json", "--history", "accept-*.json")
        check("report+history exit 0", rc == 0, (rc, out[-300:]))
        check("report printed first", out.index("ACCEPT REPORT") < out.index("ACCEPT HISTORY"),
              out[:120])
        rc, out = run("--report", "accept-1.json", "--history")
        check("bare --history defaults to accept*.json", "3 run(s)" in out, out[-300:])
        check("not-ready report exits 1", rc == 1, rc)
        rc, out = run("--history-only", "accept-*.json", "--history-limit", "1")
        check("history-limit 1", "1 run(s)" in out and "3 run(s)" not in out, out[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)}: " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
