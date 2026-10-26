# -*- coding: utf-8 -*-
"""Offline, but real: kill the process *inside* the crash window and check the disk.

`tests/test_journal.py` proves the journal logic with in-process fakes.  This suite
proves the part that actually matters when money is on the line: the line is on
disk **before** the process can die.

Two children talk to the same mock auth server over real HTTP (tests/mock_e2e.py):

  [A] "before_token": `exchange_code` sleeps forever, so the child is killed in the
      ~200 ms window after POST /register returned 303.  Expect: exactly one
      journal line (`registered`, credentials, no uid), no results file, and
      recover() reporting one unresolved entry instead of inventing a row.

  [B] "after_register": the child finishes register_one() (so the `ok` line with the
      uid is written) and then sleeps *before* the runner appends the row.  Killed
      there, it must leave two journal lines and still no results row -- and
      recover() must hand back a complete, usable row with 0 network calls.

Everything is SIGKILL (`Popen.kill`), because a graceful stop is not what loses cards.
A third part hammers the same file from 8 threads and two more processes: "lanes never
tear a line" is an O_APPEND guarantee here, not a hope.
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import mock_e2e as M  # noqa: E402
import vmax_journal as J  # noqa: E402

PY = sys.executable
fails = []
TMP = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


CHILD = r'''# -*- coding: utf-8 -*-
"""Runs one registration against the parent's mock, then dies in the window."""
import json
import sys
import time

TOOLKIT, TESTS, JOURNAL, BASE, CODES, MODE = sys.argv[1:7]
sys.path.insert(0, TOOLKIT)
sys.path.insert(0, TESTS)

import mock_e2e as M
M.Cfg.codes_path = CODES
M.Cfg.base = BASE

import vmax_register as R
from vmax_client import VmaxAuth


class SlowToken(VmaxAuth):
    """Die before the token exchange: the 303 is the last thing that happened."""

    def exchange_code(self, code, verifier, client="dreammaker"):
        time.sleep(600)
        return super().exchange_code(code, verifier, client=client)


if MODE == "before_token":
    R.VmaxAuth = SlowToken

R.build_sms = lambda spec, d: M.LatestCodeSms()

args = M.make_args()
args.base = BASE
args.redirect = BASE + "/auth/callback"
args.captcha_param = "MOCK-TOKEN"
args.journal = JOURNAL
args.sms_resends = 0

res = R.register_one(args)
print(json.dumps({"status": res.get("status"), "uid": res.get("uid")}), flush=True)
time.sleep(600)
'''


CONC = ("import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import vmax_journal as J\n"
        "p, t = sys.argv[2], sys.argv[3]\n"
        "for i in range(40):\n"
        "    phone = '137%04d%04d' % (int(t), i)\n"
        "    J.append(p, phone, 'ok', uid='P' + phone)\n")


def workdir(tag):
    d = tempfile.mkdtemp(prefix=f"jkill_{tag}_")
    TMP.append(d)
    return d


def spawn(child_script, journal, base, codes, mode):
    return subprocess.Popen([PY, "-u", child_script, ROOT, HERE, journal, base, codes, mode],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8")


def wait_for(pred, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    print("[1] mock auth server + one prepared child script")
    d = workdir("main")
    M.Cfg.codes_path = os.path.join(d, "codes.jsonl")
    M.Cfg.sms_dir = ""
    M.Cfg.cooldown = 0
    M.Cfg.phone_taken = set()
    M.Cfg.send_code_fail = {}
    M.Cfg.drop_agreements = False
    M.Cfg.require_captcha_login = True
    child = os.path.join(d, "child.py")
    with open(child, "w", encoding="utf-8") as f:
        f.write(CHILD)
    with M.Server() as srv:
        # A SIGKILLed child leaves a half-open keep-alive socket; socketserver would
        # print a ConnectionResetError traceback *after* the suite finished.  That is
        # expected noise here (it is the crash we asked for), so drop it.
        srv.httpd.handle_error = lambda *a, **k: None
        check("server is listening", srv.port > 0, srv.port)
        base = M.Cfg.base

        print("[2] killed after 303, before the token exchange")
        jp = os.path.join(d, "a.jsonl.journal")
        out = os.path.join(d, "a.jsonl")
        p = spawn(child, jp, base, M.Cfg.codes_path, "before_token")
        check("the child reached /register 303 and journaled it",
              wait_for(lambda: len(J.load(jp)) >= 1), J.load(jp))
        p.kill()
        p.wait(timeout=20)
        lines = J.load(jp)
        check("the process really died (SIGKILL)", p.returncode != 0, p.returncode)
        check("exactly one line", [r["phase"] for r in lines] == ["registered"], lines)
        check("the credentials are there", bool(lines[0].get("username")) and
              bool(lines[0].get("password")), lines[0])
        check("no uid yet (the exchange never ran)", not lines[0].get("uid"), lines[0])
        check("no access token", "access_token" not in json.dumps(lines))
        check("the row was never written", not os.path.exists(out))
        rows, unresolved = J.recover(jp, out)
        check("nothing is guessed into a row", rows == [], rows)
        check("the window is reported, not hidden",
              [u["phone"] for u in unresolved] == [lines[0]["phone"]], unresolved)

        print("[3] killed after the token exchange, before the row")
        jp2 = os.path.join(d, "b.jsonl.journal")
        out2 = os.path.join(d, "b.jsonl")
        p2 = spawn(child, jp2, base, M.Cfg.codes_path, "after_register")
        check("both lines landed before the kill",
              wait_for(lambda: len(J.load(jp2)) >= 2), J.load(jp2))
        p2.kill()
        p2.wait(timeout=20)
        lines2 = J.load(jp2)
        check("the process really died (SIGKILL)", p2.returncode != 0, p2.returncode)
        check("two lines, in order", [r["phase"] for r in lines2] == ["registered", "ok"], lines2)
        check("the uid is recorded", bool(lines2[-1].get("uid")), lines2[-1])
        check("the credentials are the same in both lines",
              lines2[0]["username"] == lines2[1]["username"] and
              lines2[0]["password"] == lines2[1]["password"], lines2)
        check("no access token", "access_token" not in json.dumps(lines2))
        check("still no results row", not os.path.exists(out2))
        rows2, unresolved2 = J.recover(jp2, out2)
        check("recover() rebuilds exactly one row", len(rows2) == 1, rows2)
        check("nothing is left unresolved", unresolved2 == [], unresolved2)
        row = rows2[0]
        check("row is ok", row["status"] == "ok", row)
        check("row carries the uid from the log", row["uid"] == lines2[-1]["uid"], row)
        check("row carries the credentials from the log",
              row["username"] == lines2[-1]["username"] and
              row["password"] == lines2[-1]["password"], row)
        check("row says where it came from", row["via"] == "journal" and
              row.get("journal_recovered") is True, row)
        check("row has no fake token", "token" not in row, row)

        print("[4] once the row exists, the log has nothing left to add")
        with open(out2, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        check("recover() is idempotent", J.recover(jp2, out2) == ([], []),
              J.recover(jp2, out2))
        check("the journal file survives (it is the evidence)", os.path.exists(jp2))

    print("[5] 8 threads + 2 processes append at once: not one torn line")
    jc = os.path.join(workdir("conc"), "conc.journal")

    lost = []

    def hammer(tag):
        for i in range(40):
            phone = "139%04d%04d" % (tag, i)
            if not J.append(jc, phone, "ok", username="u" + phone, password="p",
                            uid="U" + phone):
                lost.append(phone)

    threads = [threading.Thread(target=hammer, args=(t,)) for t in range(8)]
    procs = [subprocess.Popen([PY, "-c", CONC, ROOT, jc, str(t)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for t in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = [pr.wait(timeout=120) for pr in procs]
    check("both appender processes exited 0", codes == [0, 0], codes)
    raw = open(jc, "rb").read()
    check("the file has exactly 400 lines", raw.count(b"\n") == 400, raw.count(b"\n"))
    rows = J.load(jc)
    check("all 400 lines parse as JSON", len(rows) == 400, len(rows))
    phones = [r["phone"] for r in rows]
    check("every phone was written exactly once",
          len(set(phones)) == 400 and len(phones) == 400, len(set(phones)))
    check("every record is complete",
          all(isinstance(r.get("ts"), int) and r.get("phase") == "ok" and r.get("uid")
              for r in rows), rows[:2])
    check("no thread append was lost", lost == [], lost)
    check("no line swallowed its neighbour",
          all(r["uid"] in ("U" + r["phone"], "P" + r["phone"]) for r in rows), rows[:3])
    check("the kernel-level lock file is next to the journal",
          os.path.exists(jc + ".lock"), jc + ".lock")

    for d in TMP:
        shutil.rmtree(d, ignore_errors=True)
    print("ALL JOURNAL KILL TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
