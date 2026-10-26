# -*- coding: utf-8 -*-
"""Offline checks for the fleet ledger (no network, no token, no SMS).

The fixtures are the shapes the runners actually write: ok rows with a uid,
classified failures, phone_taken rows with a timestamp, a logins file that must
be ignored, and one unparseable line.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vmax_fleet as F  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def write_jsonl(path, rows, tail=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if tail:
            f.write(tail)


def build_fixture(td):
    now = F.today_start() + 3600          # 01:00 local today
    old = F.today_start() - 86400 * 3     # three days ago
    run1 = os.path.join(td, "runs", "run1", "results.jsonl")
    run2 = os.path.join(td, "runs", "run2", "results.jsonl")
    write_jsonl(run1, [
        {"phone": "13800000001", "status": "ok", "uid": "u-1", "ts": now},
        {"phone": "13800000002", "status": "error", "error": "code_wrong",
         "register_error_kind": "code_wrong", "ts": now},
        {"phone": "13800000003", "status": "error", "register_error_kind": "phone_taken",
         "ts": now},
       ], tail="this line is not json\n")
    write_jsonl(run2, [
        {"phone": "13800000001", "status": "ok", "uid": "u-1", "ts": now + 10},
        {"phone": "13800000004", "status": "ok", "uid": "u-2", "ts": now + 20},
        {"phone": "13800000005", "status": "ok", "uid": "u-2", "ts": now + 30},
        {"phone": "13800000006", "status": "error", "register_error_kind": "phone_taken",
         "ts": old},
        {"phone": "13800000001", "status": "ok", "uid": "u-1", "ts": now + 40},
        {"phone": "13800000007", "status": "ok", "uid": "u-3", "ts": now + 50},
        ])
    # a login re-check must not be counted as a registration
    write_jsonl(os.path.join(td, "runs", "run2", "logins.jsonl"),
                [{"phone": "13800000009", "status": "ok", "uid": "u-9"}])
    return run1, run2

def main():
    print("[1] discover: results files only, logins* skipped")
    with tempfile.TemporaryDirectory() as td:
        build_fixture(td)
        paths = F.discover(os.path.join(td, "runs"))
        names = [os.path.basename(p) for p in paths]
        check("two files found", len(paths) == 2, names)
        check("logins.jsonl skipped", "logins.jsonl" not in names, names)
        check("both are results.jsonl", names.count("results.jsonl") == 2, names)
        check("an explicit --file is added once",
              F.discover(None, [paths[0], paths[0]]) == [paths[0]])

    print("[2] totals: ok / taken / error / bad are separate")
    with tempfile.TemporaryDirectory() as td:
        build_fixture(td)
        ledger = F.build_ledger(F.discover(os.path.join(td, "runs")),
                               os.path.join(td, "runs"))
        s = F.fleet_summary(ledger)
        t = s["totals"]
        check("9 parseable rows with a phone", t["rows"] == 9, t)
        check("6 ok rows", t["ok"] == 6, t)
        check("1 error row", t["error"] == 1, t)
        check("2 taken rows", t["taken"] == 2, t)
        check("1 unparseable line", t["bad"] == 1, t)
        check("0 rows without a phone", t["skipped"] == 0, t)
        check("7 unique phones", s["unique_phones"] == 7, s["unique_phones"])
        check("4 phones with an account", s["ok_phones"] == 4, s["ok_phones"])
        check("2 phones only ever taken", s["taken_phones"] == 2, s["taken_phones"])

    print("[3] duplicate registrations and uid collisions are named")
    with tempfile.TemporaryDirectory() as td:
        build_fixture(td)
        ledger = F.build_ledger(F.discover(os.path.join(td, "runs")),
                               os.path.join(td, "runs"))
        s = F.fleet_summary(ledger)
        check("13800000001 is the only phone registered twice",
              s["duplicate_ok"] == ["13800000001"], s["duplicate_ok"])
        check("u-2 is reported under two phones",
              len(s["uid_collisions"]) == 1
              and s["uid_collisions"][0]["uid"] == "u-2"
              and s["uid_collisions"][0]["phones"] == ["13800000004", "13800000005"],
              s["uid_collisions"])
        check("a clean uid is not reported",
              all(c["uid"] != "u-1" for c in s["uid_collisions"]), s["uid_collisions"])
    print("[4] --check splits cards into ok / taken-today / was-taken / fresh")
    with tempfile.TemporaryDirectory() as td:
        build_fixture(td)
        cards = os.path.join(td, "cards.csv")
        with open(cards, "w", encoding="utf-8", newline="") as f:
            f.write("phone,username,password,proxy\n")
            f.write("13800000001,,,\n")              # already ok
            f.write("13800000003,,,\n")              # taken today
            f.write("13800000006,,,\n")              # taken 3 days ago -> eligible
            f.write("13900000001,,,\n")              # never seen
            f.write("13900000001,,,\n")              # duplicate input row
        ledger = F.build_ledger(F.discover(os.path.join(td, "runs")),
                               os.path.join(td, "runs"))
        p = F.plan(F.load_accounts(cards), ledger)
        check("duplicate input rows collapse", p["input"] == 4, p["input"])
        check("already ok", p["already_ok"] == ["13800000001"], p["already_ok"])
        check("taken today", p["taken_today"] == ["13800000003"], p["taken_today"])
        check("was taken but eligible", p["was_taken"] == ["13800000006"], p["was_taken"])
        check("fresh = eligible + never seen",
              p["fresh_phones"] == ["13800000006", "13900000001"], p["fresh_phones"])

    print("[5] today_start is a local-midnight boundary, not a 24h window")
    with tempfile.TemporaryDirectory() as td:
        start = F.today_start()
        write_jsonl(os.path.join(td, "edge", "results.jsonl"), [
            {"phone": "13700000001", "status": "error",
             "register_error_kind": "phone_taken", "ts": start + 1},
            {"phone": "13700000002", "status": "error",
             "register_error_kind": "phone_taken", "ts": start - 1},
        ])
        ledger = F.build_ledger([os.path.join(td, "edge", "results.jsonl")], None)
        cards = os.path.join(td, "c2.csv")
        with open(cards, "w", encoding="utf-8", newline="") as f:
            f.write("phone\n13700000001\n13700000002\n")
        p = F.plan(F.load_accounts(cards), ledger)
        check("one second after midnight is blocked today",
              p["taken_today"] == ["13700000001"], p["taken_today"])
        check("one second before midnight is eligible",
              p["was_taken"] == ["13700000002"], p["was_taken"])

    print("[6] --write-fresh keeps the operator's columns")
    with tempfile.TemporaryDirectory() as td:
        cards = os.path.join(td, "cards.csv")
        with open(cards, "w", encoding="utf-8", newline="") as f:
            f.write("phone,username,password,proxy\n")
            f.write("13900000001,u_one,Pw12345678,http://p:1\n")
        ledger = F.build_ledger([], None)
        p = F.plan(F.load_accounts(cards), ledger)
        out = os.path.join(td, "out", "fresh.csv")
        n = F.write_fresh(out, p["fresh"])
        with open(out, encoding="utf-8") as f:
            lines = [x.rstrip("\r\n") for x in f]
        check("one row written", n == 1 and len(lines) == 2, (n, lines))
        check("header is the batch header",
              lines[0] == "phone,username,password,proxy", lines)
        check("credentials preserved",
              lines[1] == "13900000001,u_one,Pw12345678,http://p:1", lines)
    print("[7] exit codes")
    with tempfile.TemporaryDirectory() as td:
        build_fixture(td)
        scan = os.path.join(td, "runs")
        cards = os.path.join(td, "done.csv")
        with open(cards, "w", encoding="utf-8", newline="") as f:
            f.write("phone\n13800000001\n13800000003\n")
        rc = F.main(["--scan", scan, "--check", cards, "--quiet"])
        check("all cards used -> exit 4", rc == 4, rc)
        cards2 = os.path.join(td, "mix.csv")
        with open(cards2, "w", encoding="utf-8", newline="") as f:
            f.write("phone\n13800000001\n13900000002\n")
        rc = F.main(["--scan", scan, "--check", cards2, "--quiet"])
        check("a fresh card -> exit 0", rc == 0, rc)
        rc = F.main(["--scan", os.path.join(td, "missing")])
        check("missing --scan -> exit 2", rc == 2, rc)
        empty = os.path.join(td, "empty")
        os.makedirs(empty, exist_ok=True)
        rc = F.main(["--scan", empty])
        check("no jsonl -> exit 2", rc == 2, rc)
        rc = F.main([])
        check("no inputs -> exit 2", rc == 2, rc)

    print("[8] --json is machine readable and carries no secret")
    with tempfile.TemporaryDirectory() as td:
        build_fixture(td)
        jp = os.path.join(td, "fleet.json")
        cards = os.path.join(td, "cards.csv")
        with open(cards, "w", encoding="utf-8", newline="") as f:
            f.write("phone,username,password\n13900000001,u_one,SuperSecret1\n")
        rc = F.main(["--scan", os.path.join(td, "runs"), "--check", cards,
                     "--json", jp, "--quiet"])
        raw = open(jp, encoding="utf-8").read()
        doc = json.loads(raw)
        check("exit 0 with a fresh card", rc == 0, rc)
        check("summary names the tool", doc["tool"] == "vmax_fleet", doc.get("tool"))
        check("summary has the fleet totals", doc["totals"]["ok"] == 6, doc["totals"])
        check("summary has the check block",
              doc["check"]["fresh"] == 1 and doc["check"]["already_ok"] == 0,
              doc["check"])
        check("no password in the summary", "SuperSecret1" not in raw)
        check("no access_token in the summary", "access_token" not in raw)

    print("[9] --quiet prints nothing at all")
    with tempfile.TemporaryDirectory() as td:
        build_fixture(td)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = F.main(["--scan", os.path.join(td, "runs"), "--quiet"])
        check("exit 0", rc == 0, rc)
        check("no console output", buf.getvalue() == "", buf.getvalue()[:200])

    print("ALL FLEET TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())