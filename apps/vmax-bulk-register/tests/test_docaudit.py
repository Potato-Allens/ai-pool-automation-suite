# -*- coding: utf-8 -*-
"""Offline checks for `vmax_docaudit.py` (doc numbers vs a run_all log).

The tool exists because per-suite counts live in two prose tables and drift
silently; the suite pins the parts that must not regress: UTF-16 logs (that is
what PowerShell redirection produces here), the row/count pairing, the missing-
row note, total drift, and the exit codes.
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import vmax_docaudit as D  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


LOG = """test_alpha.py                     OK    pass=10  fail=0  exit=0   0.2s
test_beta.py                      OK    pass=2   fail=0  exit=0   0.1s

TOTAL suites=2 pass=12 fail=0 bad=0 elapsed=0.3s
ALL SUITES GREEN
"""

GOOD = """| \u6587\u4ef6 | \u4f5c\u7528 |
|---|---|
| `tests/test_alpha.py` | a | 10 |
| `tests/test_beta.py` | b | 2 |
| \u79bb\u7ebf\u56de\u5f52\u603b\u91cf | **2 \u5957 12 \u9879\u65ad\u8a00** | log |
"""

DRIFT = GOOD.replace("| 10 |", "| 9 |").replace("12 \u9879", "99 \u9879")
NARROW = ("| \u5f53\u5929\u4e00\u952e | \u8dd1 `tests/test_beta.py` [7] \u9a8c\u8bc1\uff1b"
          "\u8865\u5145 | \u89c1 `tests/test_alpha.py`\uff0810 \u9879\uff09 |\r\n")


def write(path, text, enc="utf-8"):
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(text)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        log = os.path.join(td, "run.log")
        write(log, LOG, "utf-16")            # PowerShell redirection shape
        parsed = D.parse_log(log)
        print("[1] log parsing")
        check("two suites", len(parsed["suites"]) == 2, parsed["suites"])
        check("alpha=10", parsed["suites"].get("test_alpha.py") == 10, parsed["suites"])
        check("total read", parsed["total"] == {"suites": 2, "pass": 12, "fail": 0},
              parsed["total"])

        print("[2] row parsing")
        good = os.path.join(td, "good.md")
        write(good, GOOD)
        doc = D.parse_doc(good)
        check("two rows", len(doc["suites"]) == 2, doc["suites"])
        check("total captured", doc["totals"] == [(2, 12)], doc["totals"])
        narrow = os.path.join(td, "narrow.md")
        write(narrow, NARROW)
        nd = D.parse_doc(narrow)
        check("a row that names two suites belongs to the last one",
              list(nd["suites"]) == ["test_alpha.py"] and nd["suites"]["test_alpha.py"] == 10,
              nd["suites"])

        print("[3] audit verdicts")
        ok = D.audit(parsed, {"README": doc})
        check("clean doc passes", ok["ok"] is True, ok)
        bad = os.path.join(td, "bad.md")
        write(bad, DRIFT)
        res = D.audit(parsed, {"README": D.parse_doc(bad)})
        check("number drift fails", res["ok"] is False, res)
        check("drift names the suite",
              any("test_alpha.py" in p and "9" in p for p in res["problems"]), res["problems"])
        check("total drift is caught",
              any("total says" in p and "99" in p for p in res["problems"]), res["problems"])
        only_alpha = os.path.join(td, "part.md")
        write(only_alpha, "| `tests/test_alpha.py` | a | 10 |\r\n")
        res2 = D.audit(parsed, {"README": D.parse_doc(only_alpha)})
        check("a never-listed suite is a note, not a failure",
              res2["ok"] is True and any("test_beta.py" in n for n in res2["notes"]), res2)

        print("[4] exit codes")
        rc = D.main(["--log", log, "--readme", good, "--runbook", os.path.join(td, "none.md")])
        check("clean -> 0", rc == 0, rc)
        rc = D.main(["--log", log, "--readme", bad,
                     "--runbook", os.path.join(td, "none.md")])
        check("drift -> 1", rc == 1, rc)
        rc = D.main(["--log", os.path.join(td, "missing.log"), "--readme", good])
        check("unreadable log -> 2", rc == 2, rc)
        empty = os.path.join(td, "empty.log")
        write(empty, "nothing here\n")
        rc = D.main(["--log", empty, "--readme", good])
        check("a log without suite lines -> 2", rc == 2, rc)
        rc = D.main(["--log", log, "--readme", os.path.join(td, "no.md"),
                     "--runbook", os.path.join(td, "no2.md")])
        check("no docs at all -> 0 (clean-room copy)", rc == 0, rc)

    print("ALL DOCAUDIT TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
