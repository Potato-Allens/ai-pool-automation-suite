# -*- coding: utf-8 -*-
"""Do the numbers in the docs still match a real `run_all` log?

Twenty-plus rounds of "N 项" edits across README and RUNBOOK is exactly how a
doc starts lying.  This tool parses one `tests/run_all.py` log (per-suite pass
counts + TOTAL), parses the test tables of the docs, and reports:

  * rows whose number disagrees with the log,
  * suites the log has but a doc never lists,
  * stale totals ("26 套 1181 项" after the run says 27 / 1224).

    python vmax_docaudit.py --log evidence/_r128_runall.log
    python vmax_docaudit.py --log x.log --readme README.md --runbook docs/RUNBOOK.md

Exit codes: 0 everything agrees, 1 drift found, 2 an input could not be read.
Missing doc files are reported as a skip (the clean-room copy ships only *.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SUITE_LINE = re.compile(r"^(\S+\.py)\s+OK\s+pass=(\d+)", re.M)
TOTAL_LINE = re.compile(r"TOTAL suites=(\d+) pass=(\d+) fail=(\d+)")
ROW = re.compile(r"tests[\\/](\S+\.py)")
TAIL_NUM = re.compile(r"\|\s*(\d+)\s*\|\s*$")
PAREN_NUM = re.compile(r"[（(](\d+)\s*项[）)]")


def read(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_log(path: str) -> dict:
    text = read(path)
    suites = {m.group(1): int(m.group(2)) for m in SUITE_LINE.finditer(text)}
    total = None
    m = TOTAL_LINE.search(text)
    if m:
        total = {"suites": int(m.group(1)), "pass": int(m.group(2)), "fail": int(m.group(3))}
    return {"suites": suites, "total": total}


def row_number(line: str) -> int | None:
    m = TAIL_NUM.search(line)
    if m:
        return int(m.group(1))
    m = PAREN_NUM.search(line)
    if m:
        return int(m.group(1))
    return None


def parse_doc(path: str) -> dict:
    doc = {"suites": {}, "totals": []}
    for line in read(path).splitlines():
        found = ROW.findall(line)
        if found:
            # the count sits at the end of the row, so it belongs to the LAST
            # suite the row names (a row often mentions a sibling mid-sentence)
            n = row_number(line)
            if n is not None:
                doc["suites"].setdefault(found[-1], n)
        if re.search(r"\|\s*\*\*\d+ 套 \d+ 项", line):
            t = re.search(r"\*\*(\d+) 套 (\d+) 项", line)
            doc["totals"].append((int(t.group(1)), int(t.group(2))))
        elif re.search(r"全套.*?\*\*(\d+) 套 (\d+) 项", line):
            t = re.search(r"\*\*(\d+) 套 (\d+) 项", line)
            doc["totals"].append((int(t.group(1)), int(t.group(2))))
    return doc


def audit(log: dict, docs: dict) -> dict:
    problems: list[str] = []
    notes: list[str] = []
    suites = log["suites"]
    for name in sorted(docs):
        d = docs[name]
        stale = []
        for suite, n in sorted(d["suites"].items()):
            if suite not in suites:
                # legitimate with `run_all.py --only X` (a partial log), so this
                # is a note -- a *number* disagreement is the real drift
                stale.append(suite)
            elif suites[suite] != n:
                problems.append("%s: %s is %d in the doc, %d in the log"
                                % (name, suite, n, suites[suite]))
        if stale:
            notes.append("%s: %d row(s) name a suite this log did not run: %s"
                         % (name, len(stale), ", ".join(stale)))
        missing = sorted(set(suites) - set(d["suites"]))
        if missing:
            notes.append("%s: %d suite(s) never listed: %s"
                         % (name, len(missing), ", ".join(missing)))
        if log["total"]:
            want = (log["total"]["suites"], log["total"]["pass"])
            for got in d["totals"]:
                if got != want:
                    problems.append("%s: total says %d 套 %d 项, log says %d / %d"
                                    % ((name,) + got + want))
    return {"ok": not problems, "problems": problems, "notes": notes}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="doc numbers vs run_all log")
    ap.add_argument("--log", default=os.path.join(ROOT, "evidence", "_r128_runall.log"))
    ap.add_argument("--readme", default=os.path.join(ROOT, "README.md"))
    ap.add_argument("--runbook", default=os.path.join(ROOT, "docs", "RUNBOOK.md"))
    ap.add_argument("--json", default=None, help="machine summary here")
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        log = parse_log(a.log)
    except OSError as e:
        print("[docaudit] cannot read log: %s" % e, file=sys.stderr)
        return 2
    if not log["suites"]:
        print("[docaudit] no per-suite lines in %s" % a.log, file=sys.stderr)
        return 2
    docs, skipped = {}, []
    for name, path in (("README", a.readme), ("RUNBOOK", a.runbook)):
        if not os.path.isfile(path):
            skipped.append(name)
            continue
        docs[name] = parse_doc(path)
    if not docs:
        print("[docaudit] no doc to compare (skipped: %s)" % ", ".join(skipped))
        return 0
    res = audit(log, docs)
    res["log"] = a.log
    res["suites"] = len(log["suites"])
    res["checks"] = sum(len(d["suites"]) for d in docs.values())
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)) or ".", exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    for p in res["problems"]:
        print("[DRIFT] " + p)
    for n in res["notes"]:
        print("[NOTE ] " + n)
    if skipped:
        print("[docaudit] skipped (not present): %s" % ", ".join(skipped))
    if res["ok"]:
        print("[docaudit] %d suite(s) in the log, %d row(s) compared -> numbers match"
              % (res["suites"], res["checks"]))
        return 0
    print("[docaudit] %d drift(s)" % len(res["problems"]))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
