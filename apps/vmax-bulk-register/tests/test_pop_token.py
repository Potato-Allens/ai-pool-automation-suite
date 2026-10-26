# -*- coding: utf-8 -*-
"""Queue-pop correctness: one token, one worker -- threads *and* processes.

Regression for the 2026-10-26 Windows finding: renaming a file is not a usable
claim primitive under load (the same source could be renamed 2-8 times, and a
rename could also be invisible to open() right after it returned), so the pop now
runs under a mkdir-based mutex and reads the payload before claiming.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLKIT = os.path.dirname(HERE)
sys.path.insert(0, TOOLKIT)

import vmax_register as R  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def seed(d, n, prefix="tok_"):
    for i in range(n):
        p = os.path.join(d, f"{prefix}{i:04d}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"param": f"P{i:04d}", "ts": time.time()}, f)
        os.utime(p, (time.time() + i, time.time() + i))


def main():
    root = tempfile.mkdtemp(prefix="pop_")
    try:
        print("[1] eight threads, 200 tokens: every token delivered exactly once")
        d = os.path.join(root, "stress")
        os.makedirs(d)
        seed(d, 200)
        got = []
        lock = threading.Lock()

        def worker():
            while True:
                try:
                    v = R.pop_token(d, 0)
                except RuntimeError:
                    return
                with lock:
                    got.append(v)

        ths = [threading.Thread(target=worker) for _ in range(8)]
        t0 = time.time()
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        dt = time.time() - t0
        dup = [p for p, n in collections.Counter(got).items() if n > 1]
        check("200 deliveries", len(got) == 200, len(got))
        check("all unique", not dup, dup[:5])
        check("fast enough (<20 s)", dt < 20, f"{dt:.1f}s")
        names = os.listdir(d)
        check("no live token left", not [n for n in names if n.endswith(".json")], names[:5])
        check("200 used markers", sum(1 for n in names if n.endswith(".used")) == 200,
              len(names))
        check("no .used.<tag> leftovers", not [n for n in names if ".used." in n], names[:5])
        check("lock released", not os.path.exists(os.path.join(d, ".pop.lock")))

        print("[2] two processes, 100 tokens: still no double delivery")
        d2 = os.path.join(root, "procs")
        os.makedirs(d2)
        seed(d2, 100)
        code = (
            "import json,sys\n"
            f"sys.path.insert(0, {TOOLKIT!r})\n"
            "import vmax_register as R\n"
            "out=[]\n"
            "for _ in range(int(sys.argv[2])):\n"
            "    try: out.append(R.pop_token(sys.argv[1],0))\n"
            "    except RuntimeError: break\n"
            "print(json.dumps(out))\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", code, d2, "60"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8") for _ in range(2)]
        outs = []
        for p in procs:
            o, e = p.communicate(timeout=180)
            outs.append(json.loads(o.strip().splitlines()[-1]))
        merged = [x for o in outs for x in o]
        check("120 attempts, 100 tokens", len(merged) == 100, len(merged))
        check("cross-process unique", len(set(merged)) == 100, len(set(merged)))
        check("both processes got work", all(o for o in outs), [len(o) for o in outs])

        print("[3] unreadable tokens are left alone, not burned")
        d3 = os.path.join(root, "bad")
        os.makedirs(d3)
        bad = os.path.join(d3, "tok_0000.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        empty = os.path.join(d3, "tok_0001.json")
        with open(empty, "w", encoding="utf-8") as f:
            json.dump({"param": ""}, f)
        good = os.path.join(d3, "tok_0002.json")
        with open(good, "w", encoding="utf-8") as f:
            json.dump({"param": "OK"}, f)
        for p in (bad, empty, good):
            os.utime(p, (time.time() - 1, time.time() - 1))
        check("good token wins", R.pop_token(d3, 0) == "OK")
        names = sorted(os.listdir(d3))
        check("bad json untouched", "tok_0000.json" in names, names)
        check("empty param untouched", "tok_0001.json" in names, names)
        try:
            R.pop_token(d3, 0)
            check("nothing readable raises", False)
        except RuntimeError as e:
            check("nothing readable raises", "none readable" in str(e), str(e))

        print("[4] stale tokens are retired, not handed out")
        d4 = os.path.join(root, "stale")
        os.makedirs(d4)
        stale = os.path.join(d4, "tok_0000.json")
        with open(stale, "w", encoding="utf-8") as f:
            json.dump({"param": "OLD"}, f)
        os.utime(stale, (time.time() - 500, time.time() - 500))
        fresh = os.path.join(d4, "tok_0001.json")
        with open(fresh, "w", encoding="utf-8") as f:
            json.dump({"param": "NEW"}, f)
        os.utime(fresh, (time.time(), time.time()))
        old = os.environ.get("VMAX_TOKEN_MAX_AGE")
        os.environ["VMAX_TOKEN_MAX_AGE"] = "90"
        try:
            got = R.pop_token(d4, 0)
        finally:
            if old is None:
                os.environ.pop("VMAX_TOKEN_MAX_AGE", None)
            else:
                os.environ["VMAX_TOKEN_MAX_AGE"] = old
        check("fresh token served, stale skipped", got == "NEW", got)
        check("stale token retired", os.path.exists(stale + ".stale"), sorted(os.listdir(d4)))

        print("[5] wait>0 blocks for the farm instead of failing")
        d5 = os.path.join(root, "wait")
        os.makedirs(d5)

        def producer():
            time.sleep(1.2)
            with open(os.path.join(d5, "tok_0000.json"), "w", encoding="utf-8") as f:
                json.dump({"param": "LATE"}, f)

        threading.Thread(target=producer, daemon=True).start()
        t0 = time.time()
        v = R.pop_token(d5, 5)
        waited = time.time() - t0
        check("got the late token", v == "LATE", v)
        check("really waited (~1.2 s)", 1.0 <= waited < 5.0, f"{waited:.2f}s")

        print("[6] pop log is opt-in")
        d6 = os.path.join(root, "log")
        os.makedirs(d6)
        seed(d6, 3)
        logp = os.path.join(root, "pop.jsonl")
        old = os.environ.pop("VMAX_POP_LOG", None)
        try:
            R.pop_token(d6, 0)
            check("no log without the env var", not os.path.exists(logp))
            os.environ["VMAX_POP_LOG"] = logp
            R.pop_token(d6, 0)
            R.pop_token(d6, 0)
            rows = [json.loads(l) for l in open(logp, encoding="utf-8") if l.strip()]
            check("two lines for two pops", len(rows) == 2, len(rows))
            check("records the source", all(r.get("src", "").endswith(".json") for r in rows), rows)
            check("records the outcome", all(r.get("outcome") == "ok" for r in rows), rows)
        finally:
            os.environ.pop("VMAX_POP_LOG", None)
            if old:
                os.environ["VMAX_POP_LOG"] = old
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)}: " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
