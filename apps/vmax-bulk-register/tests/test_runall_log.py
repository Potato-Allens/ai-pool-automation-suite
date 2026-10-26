# -*- coding: utf-8 -*-
"""Offline checks for `tests/run_all.py --log` (R132).

Why this exists: the docs tell you to produce a regression log with `--log`, and
then to audit it with `vmax_docaudit.py`.  But `--log` used to write only the raw
per-suite output plus a TOTAL line -- no `name OK pass=N` rows -- so the documented
audit command died with "no per-suite lines".  These checks pin the contract:

* the file carries `[run_all] N suite(s)`, each `===== name =====` block, the
  matching `name OK pass=.. fail=..` line, TOTAL and the verdict;
* `--quiet` changes the console only, never the file;
* a red suite still lands in the file as `FAILED: name`;
* `vmax_docaudit.parse_log()` reads the file with no post-processing.

Everything runs a real subprocess of the runner (`--only watchdog`, ~0.3 s);
no network, no SMS, no captcha.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import run_all as R      # noqa: E402
import vmax_docaudit as D  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def res(name, ok, npass, nfail=0, secs=0.2, code=0):
    return {"name": name, "exit": code, "pass": npass, "fail": nfail,
            "seconds": secs, "out": "", "timeout": False, "green": ok}


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def run_runner(args, logpath):
    cp = subprocess.run([sys.executable, "-u", os.path.join("tests", "run_all.py")] + args,
                        cwd=ROOT, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=300)
    return cp


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        # ---- [1] synthetic green log -------------------------------------
        print("[1] build_log: green")
        items = [
            (f"===== test_alpha.py (exit=0 pass=10 fail=0) =====\n[1] block", res("test_alpha.py", True, 10)),
            (f"===== test_beta.py (exit=0 pass=2 fail=0) =====\n[2] block", res("test_beta.py", True, 2)),
        ]
        text = R.build_log("[run_all] 2 suite(s), pacer state per suite in X", items, 0.3)
        log = os.path.join(td, "green.log")
        write(log, text)
        check("header is line 1", text.splitlines()[0].startswith("[run_all] 2 suite(s)"), text.splitlines()[:1])
        check("both blocks kept", "===== test_alpha.py" in text and "===== test_beta.py" in text)
        check("summary row shape", "test_alpha.py" in text and "  OK  " in text and "pass=10" in text)
        check("TOTAL counts", "TOTAL suites=2 pass=12 fail=0 bad=0" in text, text.splitlines()[-2:])
        check("green verdict", text.rstrip().endswith("ALL SUITES GREEN"), text.splitlines()[-1:])
        check("no FAILED line on green", "FAILED:" not in text)

        # ---- [2] synthetic red log ---------------------------------------
        print("[2] build_log: red")
        items = [
            (f"===== test_alpha.py (exit=0 pass=10 fail=0) =====\nok", res("test_alpha.py", True, 10)),
            (f"===== test_beta.py (exit=1 pass=2 fail=3) =====\n  FAIL boom", res("test_beta.py", False, 2, 3, 0.4, 1)),
        ]
        text2 = R.build_log("[run_all] 2 suite(s), pacer state per suite in X", items, 0.6)
        red = os.path.join(td, "red.log")
        write(red, text2)
        check("FAILED names the suite", "FAILED: test_beta.py" in text2, text2.splitlines()[-2:])
        check("no green verdict on red", "ALL SUITES GREEN" not in text2)
        check("red row says FAIL", any(l.startswith("test_beta.py") and "FAIL" in l.split()[1]
                                       for l in text2.splitlines() if l.startswith("test_beta.py")),
              [l for l in text2.splitlines() if l.startswith("test_beta.py")])
        check("TOTAL keeps the fail count", "TOTAL suites=2 pass=12 fail=3 bad=1" in text2,
              text2.splitlines()[-2:])

        # ---- [3] docaudit parses the file as-is --------------------------
        print("[3] vmax_docaudit reads the log directly")
        got = D.parse_log(log)
        check("two suites parsed", got["suites"] == {"test_alpha.py": 10, "test_beta.py": 2}, got["suites"])
        check("total parsed", got["total"] == {"suites": 2, "pass": 12, "fail": 0}, got["total"])

        # ---- [4] real subprocess, default console ------------------------
        print("[4] real run: --only watchdog --log")
        real = os.path.join(td, "real.log")
        cp = run_runner(["--only", "watchdog", "--log", real], real)
        body = open(real, "rb").read().decode("utf-8")
        parsed = D.parse_log(real)
        check("runner exit 0", cp.returncode == 0, cp.returncode)
        check("log has a summary row for the suite",
              list(parsed["suites"]) == ["test_watchdog.py"], parsed["suites"])
        check("log has TOTAL suites=1", (parsed["total"] or {}).get("suites") == 1, parsed["total"])
        check("log ends with the verdict", body.rstrip().endswith("ALL SUITES GREEN"), body.splitlines()[-1:])
        n = parsed["suites"].get("test_watchdog.py", 0)
        check("suite really ran (pass>0)", n > 0, n)

        # ---- [5] the documented audit command works on it -----------------
        print("[5] vmax_docaudit.py --log <that file>")
        md = os.path.join(td, "doc.md")
        write(md, "| 文件 | 作用 |\n|---|---|\n"
                  f"| `tests/test_watchdog.py` | w | {n} |\n"
                  f"| 离线回归总量 | **1 套 {n} 项断言** | log |\n")
        rc = D.main(["--log", real, "--readme", md, "--runbook", os.path.join(td, "none.md")])
        check("numbers match -> exit 0", rc == 0, rc)

        # ---- [6] --quiet must not change the file ------------------------
        print("[6] --quiet only silences the console")
        quiet = os.path.join(td, "quiet.log")
        cp2 = run_runner(["--only", "watchdog", "--log", quiet, "--quiet"], quiet)
        qbody = open(quiet, "rb").read().decode("utf-8")
        qparsed = D.parse_log(quiet)
        check("quiet run exit 0", cp2.returncode == 0, cp2.returncode)
        check("no PASS detail on the console", "  PASS " not in cp2.stdout, cp2.stdout[:120])
        check("console still prints the suite row", "test_watchdog.py" in cp2.stdout, cp2.stdout[-160:])
        check("quiet log still has the summary", qparsed["suites"] == parsed["suites"], qparsed["suites"])
        check("quiet log still has TOTAL", (qparsed["total"] or {}).get("suites") == 1, qparsed["total"])
        check("quiet log still has the verdict", qbody.rstrip().endswith("ALL SUITES GREEN"))

    print("ALL RUNALL-LOG TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
