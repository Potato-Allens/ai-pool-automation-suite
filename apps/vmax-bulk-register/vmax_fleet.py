# -*- coding: utf-8 -*-
"""One ledger across every run on disk: never pay twice for the same number.

    python vmax_fleet.py --scan runs
    python vmax_fleet.py --scan runs --check cards.csv
    python vmax_fleet.py --scan runs --check cards.csv --write-fresh fresh.csv
    python vmax_fleet.py --scan runs --json fleet.json

Why this exists
---------------
``vmax_report.py`` reads one file and ``vmax_go`` dedupes inside one ``--out``.
A real day is split across shards, days, the panel runner and reruns -- and the
same number can be paid for twice (one token at send-code, or a full
registration).  The fleet ledger answers the two questions an operator actually
has:

* before a batch: which of these phones is already done somewhere? (``--check``)
* after a batch: what does the whole fleet look like? (the summary)

Rows without a ``phone`` are counted as ``skipped`` (e.g. accept logs); lines
that are not JSON are counted as ``bad``, never fatal.  ``logins*.jsonl`` is
skipped by name: it is the password re-check of a run, not a registration.

Nothing secret is printed: the machine summary and the console never contain
passwords or access tokens.  ``--write-fresh`` writes the operator's own card
rows (including their password column) because that file is meant to be fed
straight back into ``vmax_go``.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vmax_batch import load_accounts  # noqa: E402
from vmax_report import is_taken  # noqa: E402


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def discover(scan: str | None, files: list[str] | None = None) -> list[str]:
    """Every results-like .jsonl under *scan*, plus any explicitly named file."""
    out: list[str] = []
    if scan:
        for root, dirs, names in os.walk(scan):
            dirs.sort()
            for name in sorted(names):
                if not name.endswith(".jsonl"):
                    continue
                if name.startswith("logins"):     # login re-check, not a registration
                    continue
                if name.endswith(".prev"):
                    continue
                out.append(os.path.join(root, name))
    for f in files or []:
        if f not in out:
            out.append(f)
    return out


def read_rows(path: str) -> tuple[list[dict], int, int]:
    """(rows with a phone, rows without a phone, unparseable lines)."""
    rows, skipped, bad = [], 0, 0
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return [], 0, 1
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(rec, dict):
                bad += 1
                continue
            phone = str(rec.get("phone") or rec.get("mobile") or "").strip()
            if not phone:
                skipped += 1
                continue
            rec["phone"] = phone
            rows.append(rec)
    return rows, skipped, bad


def _ts(rec: dict) -> float | None:
    v = rec.get("ts")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _label(path: str, scan: str | None) -> str:
    if scan:
        try:
            return os.path.relpath(path, scan).replace(os.sep, "/")
        except ValueError:
            pass
    return os.path.basename(path)


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def build_ledger(paths: list[str], scan: str | None = None) -> dict:
    """phone -> what we know about it, plus the fleet totals."""
    phones: dict[str, dict] = {}
    uids: dict[str, set] = {}
    files: list[dict] = []
    totals = {"rows": 0, "skipped": 0, "bad": 0, "ok": 0, "error": 0, "taken": 0}
    span: list[float] = []

    for path in paths:
        rows, skipped, bad = read_rows(path)
        label = _label(path, scan)
        f_ok = f_taken = f_err = 0
        for rec in rows:
            phone = rec["phone"]
            p = phones.setdefault(phone, {"ok": [], "taken": [], "first": None, "last": None,
                                          "taken_last": None, "uids": set()})
            ts = _ts(rec)
            if ts is not None:
                span.append(ts)
                if p["first"] is None or ts < p["first"]:
                    p["first"] = ts
                if p["last"] is None or ts > p["last"]:
                    p["last"] = ts
            if rec.get("status") == "ok":
                totals["ok"] += 1
                f_ok += 1
                if label not in p["ok"]:
                    p["ok"].append(label)
                uid = str(rec.get("uid") or "").strip()
                if uid:
                    uids.setdefault(uid, set()).add(phone)
                    p["uids"].add(uid)
            elif is_taken(rec):
                totals["taken"] += 1
                f_taken += 1
                if label not in p["taken"]:
                    p["taken"].append(label)
                if ts is not None and (p["taken_last"] is None or ts > p["taken_last"]):
                    p["taken_last"] = ts
            else:
                totals["error"] += 1
                f_err += 1
        totals["rows"] += len(rows)
        totals["skipped"] += skipped
        totals["bad"] += bad
        files.append({"file": label, "rows": len(rows), "ok": f_ok,
                      "taken": f_taken, "error": f_err, "bad": bad})

    ok_phones = {ph for ph, p in phones.items() if p["ok"]}
    taken_phones = {ph for ph, p in phones.items() if p["taken"] and not p["ok"]}
    dup_ok = sorted(ph for ph, p in phones.items() if len(p["ok"]) > 1)
    collisions = sorted((uid, sorted(phs)) for uid, phs in uids.items() if len(phs) > 1)

    return {
        "files": files,
        "totals": totals,
        "phones": phones,
        "ok_phones": ok_phones,
        "taken_phones": taken_phones,
        "duplicate_ok": dup_ok,
        "uid_collisions": collisions,
        "span": (round(max(span) - min(span), 1) if len(span) > 1 else 0.0),
        "earliest_ts": (int(min(span)) if span else None),
        "latest_ts": (int(max(span)) if span else None),
    }


def fleet_summary(ledger: dict) -> dict:
    """JSON-safe view (no phone list duplication, no secrets)."""
    t = dict(ledger["totals"])
    return {
        "tool": "vmax_fleet", "ts": int(time.time()),
        "files": ledger["files"], "totals": t,
        "unique_phones": len(ledger["phones"]),
        "ok_phones": len(ledger["ok_phones"]),
        "taken_phones": len(ledger["taken_phones"]),
        "duplicate_ok": ledger["duplicate_ok"][:200],
        "uid_collisions": [{"uid": u, "phones": ph[:20]}
                           for u, ph in ledger["uid_collisions"][:50]],
        "span_seconds": ledger["span"],
        "earliest_ts": ledger["earliest_ts"], "latest_ts": ledger["latest_ts"],
    }


# --------------------------------------------------------------------------- #
# --check: what is safe to run tonight
# --------------------------------------------------------------------------- #
def today_start(now: float | None = None) -> float:
    lt = time.localtime(time.time() if now is None else now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def plan(accounts: list[dict], ledger: dict, now: float | None = None) -> dict:
    """Split the operator's cards into ok / taken-today / was-taken / fresh.

    ``phone_taken`` only blocks *the day it was seen* (R105): a number taken
    three days ago may be recyclable with ``--on-taken reset``, so it is
    reported separately and still handed to the operator as ``was_taken``.
    """
    start = today_start(now)
    phones = ledger["phones"]
    fresh, taken_today, was_taken, already = [], [], [], []
    seen = set()
    for acc in accounts:
        ph = acc["phone"]
        if ph in seen:                     # duplicate rows in the input file
            continue
        seen.add(ph)
        p = phones.get(ph)
        if p and p["ok"]:
            already.append(ph)
        elif p and p["taken_last"] is not None and p["taken_last"] >= start:
            taken_today.append(ph)
        elif p and p["taken"]:
            was_taken.append(ph)
            fresh.append(acc)
        else:
            fresh.append(acc)
    return {"input": len(seen), "already_ok": already, "taken_today": taken_today,
            "was_taken": was_taken, "fresh": fresh, "fresh_phones": [a["phone"] for a in fresh]}


def write_fresh(path: str, fresh: list[dict]) -> int:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone", "username", "password", "proxy"])
        for a in fresh:
            w.writerow([a["phone"], a.get("username") or "", a.get("password") or "",
                        a.get("proxy") or ""])
    return len(fresh)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="one ledger across every vmax run on disk")
    ap.add_argument("--scan", default=None, help="directory walked for *.jsonl (logins* skipped)")
    ap.add_argument("--file", action="append", default=[],
                    help="extra results file (repeatable)")
    ap.add_argument("--check", default=None,
                    help="cards.csv/.jsonl to classify against the ledger")
    ap.add_argument("--write-fresh", default=None,
                    help="write the fresh cards (phone,username,password,proxy) here")
    ap.add_argument("--json", default=None, help="write the machine summary here")
    ap.add_argument("--quiet", action="store_true", help="only the exit code and --json")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    ns = ap.parse_args(argv)

    if not ns.scan and not ns.file:
        print("[fleet] give --scan DIR and/or --file results.jsonl", file=sys.stderr)
        return 2
    if ns.scan and not os.path.isdir(ns.scan):
        print("[fleet] --scan is not a directory: %s" % ns.scan, file=sys.stderr)
        return 2

    paths = discover(ns.scan, ns.file)
    if not paths:
        print("[fleet] no .jsonl under %s" % (ns.scan or "(no --scan)"), file=sys.stderr)
        return 2

    ledger = build_ledger(paths, ns.scan)
    s = fleet_summary(ledger)
    if not ns.quiet:
        t = s["totals"]
        print("[fleet] %d file(s), %d row(s) with a phone (%d without, %d unparseable)"
              % (len(s["files"]), t["rows"], t["skipped"], t["bad"]), flush=True)
        print("[fleet] ok rows=%d phones=%d | taken rows=%d phones=%d | error rows=%d"
              % (t["ok"], s["ok_phones"], t["taken"], s["taken_phones"], t["error"]), flush=True)
        if s["duplicate_ok"]:
            print("[fleet] %d phone(s) registered ok in more than one file: %s"
                  % (len(s["duplicate_ok"]), ", ".join(s["duplicate_ok"][:5])), flush=True)
        else:
            print("[fleet] no phone was registered twice", flush=True)
        if s["uid_collisions"]:
            print("[fleet] WARNING %d uid(s) appear under more than one phone"
                  % len(s["uid_collisions"]), flush=True)
        else:
            print("[fleet] no uid collision", flush=True)

    code = 0
    if ns.check:
        accounts = load_accounts(ns.check)
        p = plan(accounts, ledger)
        s["check"] = {"input": p["input"], "already_ok": len(p["already_ok"]),
                      "taken_today": len(p["taken_today"]),
                      "was_taken": len(p["was_taken"]), "fresh": len(p["fresh"]),
                      "fresh_phones": p["fresh_phones"][:500],
                      "file": os.path.abspath(ns.check)}
        if not ns.quiet:
            print("[fleet] --check %s: %d row(s) -> fresh %d, already ok %d, "
                  "taken today %d, was taken (eligible) %d"
                  % (ns.check, p["input"], len(p["fresh"]), len(p["already_ok"]),
                     len(p["taken_today"]), len(p["was_taken"])), flush=True)
        if ns.write_fresh:
            n = write_fresh(ns.write_fresh, p["fresh"])
            s["check"]["fresh_file"] = os.path.abspath(ns.write_fresh)
            if not ns.quiet:
                print("[fleet] wrote %d fresh card(s) -> %s" % (n, ns.write_fresh), flush=True)
        if not p["fresh"]:
            if not ns.quiet:
                print("[fleet] nothing to do (every card is already ok or taken today)", flush=True)
            code = 4

    if ns.json:
        d = os.path.dirname(os.path.abspath(ns.json))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(ns.json, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
        if not ns.quiet:
            print("[fleet] summary -> %s" % ns.json, flush=True)

    return code


if __name__ == "__main__":
    raise SystemExit(main())
