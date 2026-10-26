# -*- coding: utf-8 -*-
"""Mid-run SMS watchdog (R118): a channel that dies at hour 2 must stop the run.

A batch can last hours.  Before R118 the receiver was probed once, at the start,
so a daemon that died later cost **one token and one 5-minute wait per remaining
number** -- and the operator only saw it at the end.  These cases drive the real
`vmax_go.main` / `vmax_batch.main` loops in-process with `preflight`, `run_one` /
`register_one` and `channel_check` replaced, so the only thing under test is the
loop: 0 network, 0 SMS, no timing races.

    [1] go: dies after 2 rows          -> 2 rows, exit 9, summary flags it
    [2] go: --sms-recheck 0 0          -> only the start gate probes, 3 rows, exit 0
    [3] go: --no-sms-check             -> no probe at all, 3 rows, exit 0
    [4] batch, 2 lanes: dies at 2      -> exit 9, 2 rows, 2 rows never attempted
    [5] batch, 1 lane: dies at 2       -> exit 9, the third row is never touched
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vmax_batch  # noqa: E402
import vmax_go  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def write_cards(path, phones):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("phone,username,password,proxy\n")
        for p in phones:
            f.write("%s,,,\n" % p)


def read_rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8-sig") if l.strip()]


class FakeChannel:
    """A channel whose answers are scripted, and which counts the probes."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.probes = 0

    def __call__(self, spec, directory="sms_inbox", timeout=6.0):
        ok = self.answers[self.probes] if self.probes < len(self.answers) else self.answers[-1]
        self.probes += 1
        return {"ok": bool(ok), "detail": "fake up" if ok else "fake down",
                "provider": "FakeSms"}


def run_go(tmp, name, phones, channel, extra=()):
    cards = os.path.join(tmp, name + ".csv")
    write_cards(cards, phones)
    out = os.path.join(tmp, name, "results.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    seen = []

    def fake_run_one(acc, args):
        seen.append(acc["phone"])
        return {"phone": acc["phone"], "status": "ok", "uid": "u-" + acc["phone"],
                "ts": 0, "register": {"status": 303},
                "token": {"access_token": "t", "token_type": "Bearer"}}

    old = (vmax_go.channel_check, vmax_go.run_one, vmax_go.preflight)
    vmax_go.channel_check = channel
    vmax_go.run_one = fake_run_one
    vmax_go.preflight = lambda *a, **k: []
    try:
        rc = vmax_go.main(["--input", cards, "--out", out, "--captcha", "param",
                           "--captcha-param", "TOK", "--sms", "fake:chan", "--yes"]
                          + list(extra))
    finally:
        vmax_go.channel_check, vmax_go.run_one, vmax_go.preflight = old
    summary = {}
    sp = out + ".summary.json"
    if os.path.exists(sp):
        summary = json.load(open(sp, encoding="utf-8"))
    return rc, read_rows(out), summary, seen


def run_batch(tmp, name, phones, channel, extra=(), inside=None):
    cards = os.path.join(tmp, name + ".csv")
    write_cards(cards, phones)
    out = os.path.join(tmp, name, "results.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    seen = []

    def fake_register_one(ns):
        if inside is not None:
            inside(ns)          # let the test hold a row "in flight"
        seen.append(ns.phone)
        return {"phone": ns.phone, "status": "ok", "uid": "u-" + ns.phone, "ts": 0}

    old = (vmax_batch.channel_check, vmax_batch.register_one, sys.argv)
    vmax_batch.channel_check = channel
    vmax_batch.register_one = fake_register_one
    sys.argv = ["vmax_batch.py", "--input", cards, "--out", out,
                "--captcha", "param", "--captcha-param", "TOK",
                "--sms", "fake:chan", "--delay", "0"] + list(extra)
    try:
        rc = vmax_batch.main()
    finally:
        vmax_batch.channel_check, vmax_batch.register_one = old[0], old[1]
        sys.argv = old[2]
    return rc, read_rows(out), seen

def main() -> int:
    tmp = tempfile.mkdtemp(prefix="watchdog_")
    phones4 = ["13700137201", "13700137202", "13700137203", "13700137204"]
    phones3 = phones4[:3]

    print("[1] go: the channel dies after two rows")
    ch1 = FakeChannel([True, True, False])
    rc, rows, summary, seen = run_go(tmp, "go1", phones3, ch1,
                                     extra=("--sms-recheck", "1"))
    check("1 exit 9", rc == 9, rc)
    check("1 two rows written", len(rows) == 2, len(rows))
    check("1 the third number was never attempted", seen == phones3[:2], seen)
    check("1 summary says why", summary.get("stopped_on_sms_down") is True, summary)
    # The counter is the watchdog's own probes (the start gate is not counted):
    # one ok after the first row, one failing after the second.
    check("1 summary counts the watchdog probes",
          (summary.get("sms_recheck") or {}).get("probes") == 2, summary.get("sms_recheck"))
    check("1 rows are resumable (no error rows)",
          all(r.get("status") == "ok" for r in rows), rows)

    print("[2] go: --sms-recheck 0 --sms-recheck-seconds 0 -> only the start gate probes")
    ch2 = FakeChannel([True])
    rc, rows, summary, seen = run_go(tmp, "go2", phones3, ch2,
                                     extra=("--sms-recheck", "0", "--sms-recheck-seconds", "0"))
    check("2 exit 0", rc == 0, rc)
    check("2 all three rows", len(rows) == 3, len(rows))
    check("2 exactly one probe (the gate)", ch2.probes == 1, ch2.probes)
    check("2 summary does not claim a stop", summary.get("stopped_on_sms_down") is False, summary)

    print("[3] go: --no-sms-check disables both the gate and the watchdog")
    ch3 = FakeChannel([False])
    rc, rows, summary, seen = run_go(tmp, "go3", phones3, ch3,
                                     extra=("--no-sms-check", "--sms-recheck", "1"))
    check("3 exit 0", rc == 0, rc)
    check("3 all three rows", len(rows) == 3, len(rows))
    check("3 zero probes", ch3.probes == 0, ch3.probes)

    print("[4] batch with two lanes: dies at the first watchdog probe")
    ch4 = FakeChannel([True, False])
    # Without this the test gambles: lane A can finish, probe, and flip the
    # channel to "down" before lane B's thread is even dispatched -- then only
    # one row lands and the assertion fails for a scheduling reason.  The point
    # of [4] is "a lane already in flight is not interrupted", so put both lanes
    # in flight before either returns (R152).
    gate = threading.Barrier(2, timeout=10)
    rc, rows, seen = run_batch(tmp, "b4", phones4, ch4,
                               extra=("--workers", "2", "--sms-recheck", "1"),
                               inside=lambda ns: gate.wait())
    check("4 exit 9", rc == 9, rc)
    check("4 two rows (one per lane)", len(rows) == 2, len(rows))
    check("4 two numbers were never attempted", len(seen) == 2, seen)
    check("4 the probe count is the gate plus one", ch4.probes == 2, ch4.probes)

    print("[5] batch on one lane: the third row is never touched")
    ch5 = FakeChannel([True, True, False])
    rc, rows, seen = run_batch(tmp, "b5", phones3, ch5, extra=("--sms-recheck", "1"))
    check("5 exit 9", rc == 9, rc)
    check("5 two rows", len(rows) == 2, len(rows))
    check("5 the third number was not run", seen == phones3[:2], seen)

    print("ALL WATCHDOG TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
