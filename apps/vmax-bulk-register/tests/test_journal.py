# -*- coding: utf-8 -*-
"""Offline: the write-ahead log makes a crash between "303" and "row on disk" free.

`register_one()` gets 303 -> the account exists.  The row reaches results.jsonl
only when the caller returns, so a Ctrl-C / killed lane / OOM in that window used
to lose the generated password *and* hide the fact that the number is taken: the
next run pays a reset SMS just to get back in.

This suite drives the real `register_one()` and the real `vmax_go.main()` /
`vmax_batch.main()` (preflight and the network are stubbed) and checks:

  [1] append/load round trip, no token in the log
  [2] latest() prefers "ok" and never demotes it
  [3] torn tail / garbage / missing file / nested path
  [4] synthesize() builds a row results.jsonl can hold
  [5] recover() skips phones that already have a row (any status)
  [6] a "registered" line without a uid is reported, not guessed at
  [7] register_one writes both lines on success, none on failure
  [8] vmax_go recovers for free and only registers the genuinely new number
  [9] vmax_batch does the same with two lanes
 [10] --no-journal writes nothing and recovers nothing
 [11] a pool that was fully recovered exits 0, not 4
 [12] vmax_day re-verifies recovered rows (a corrupt summary is tolerated)
 [13] vmax_fleet counts a recovered row like any other ok row
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vmax_journal as J  # noqa: E402
import vmax_register as R  # noqa: E402
from vmax_client import VmaxAuth as RealAuth  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


TMP = []


def workdir(tag):
    d = tempfile.mkdtemp(prefix=f"journal_{tag}_")
    TMP.append(d)
    return d


OK = {"status": 303,
      "location": "https://dreammaker.vobilemax.cn/auth/callback?code=THECODE&state=st",
      "code": "THECODE"}


class FakeSms:
    def wait(self, phone, timeout=1, since=None):
        return "123456"


class FakeCli:
    base = "https://auth.vobilemax.cn"
    redirect_uri = None

    def __init__(self, form_result):
        self.form_result = form_result
        self.form_calls = 0

    def client_conf(self, client="dreammaker"):
        return {"client_id": "dreamstudio", "platform": "dreammaker",
                "redirect_uri": "https://dreammaker.vobilemax.cn/auth/callback"}

    def send_register_code(self, phone, param, client="dreammaker"):
        return {"ok": True, "message": "验证码已发送"}

    def register_form(self, phone, code, username, password, verifier, challenge, state,
                      captcha_verify_param, invitation_code="", client="dreammaker",
                      omit_sms_code=False):
        self.form_calls += 1
        return dict(self.form_result)

    def exchange_code(self, code, verifier, client="dreammaker"):
        return {"access_token": "AT", "expires_in": 3600, "user": {"uid": "U1"}}


class FakeAuth:
    _inst = None
    login_url = staticmethod(RealAuth.login_url)

    def __new__(cls, *a, **k):
        return cls._inst


def register(journal_path, form_result=None):
    cli = FakeCli(form_result or OK)
    FakeAuth._inst = cli
    R.VmaxAuth = FakeAuth
    R.obtain_captcha = lambda args, url: "TOKEN1"
    R.build_sms = lambda spec, d: FakeSms()
    args = SimpleNamespace(
        phone="13012345678", username=None, password=None, platform="dreammaker",
        invite="", captcha="param", captcha_param="T", captcha_attempts=1,
        queue_dir="tokens", queue_wait=0, assist_dir="assist", stdin=False,
        headed=False, sms="none", sms_dir="sms_inbox", sms_timeout=1,
        sms_resends=0, proxy=None, debug_dir=None, out=None, base=None, redirect=None,
        allow_virtual=False, form_captcha="auto", retry_503=0, wait_503=0,
        journal=journal_path)
    try:
        return R.register_one(args), cli, None
    except Exception as e:  # noqa: BLE001
        return getattr(e, "result", None), cli, e


def write_csv(path, phones):
    with open(path, "w", encoding="utf-8") as f:
        f.write("phone\n")
        for ph in phones:
            f.write(ph + "\n")


def read_rows(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main() -> int:
    print("[1] append/load round trip")
    d = workdir("rt")
    p = os.path.join(d, "results.jsonl.journal")
    check("default path is <out>.journal", J.default_path("results.jsonl") == "results.jsonl.journal")
    check("first line lands", J.append(p, "13800000001", "registered", username="u1", password="p1"))
    check("second line lands", J.append(p, "13800000001", "ok", username="u1", password="p1", uid="UID1"))
    rows = J.load(p)
    check("two records", [r["phase"] for r in rows] == ["registered", "ok"], rows)
    check("uid only on the second", rows[0].get("uid") is None and rows[1]["uid"] == "UID1", rows)
    check("no access token in the log", "access_token" not in json.dumps(rows))
    check("ts is a timestamp", isinstance(rows[0]["ts"], int) and rows[0]["ts"] > 1_600_000_000)
    check("append refuses empty input quietly",
          J.append(None, "1", "ok") is False and J.append(p, "", "ok") is False)

    print("[2] latest(): ok wins, and is never demoted")
    best = J.latest(p)
    check("one record per phone", list(best) == ["13800000001"], list(best))
    check("phase ok", best["13800000001"]["phase"] == "ok")
    J.append(p, "13800000002", "registered", username="u2", password="p2a")
    J.append(p, "13800000002", "registered", username="u2", password="p2b")
    check("later registered wins", J.latest(p)["13800000002"]["password"] == "p2b",
          J.latest(p)["13800000002"])
    J.append(p, "13800000001", "registered", username="u1", password="p1")
    check("a trailing registered line does not demote ok",
          J.latest(p)["13800000001"]["phase"] == "ok", J.latest(p)["13800000001"])

    print("[3] a torn tail / garbage / missing file never raises")
    q = os.path.join(d, "torn.journal")
    with open(q, "w", encoding="utf-8") as f:
        f.write('{"phone":"1","phase":"ok","uid":"U"}\n{"phone":"2","pha')
    check("torn tail dropped", [r["phone"] for r in J.load(q)] == ["1"], J.load(q))
    with open(q, "a", encoding="utf-8") as f:
        f.write("not json\n[1,2]\n")
    check("garbage lines dropped", [r["phone"] for r in J.load(q)] == ["1"], J.load(q))
    check("missing file is empty", J.load(os.path.join(d, "nope.journal")) == [])
    nested = os.path.join(d, "a", "b", "x.journal")
    check("nested path is created", J.append(nested, "9", "ok", uid="U9"))
    check("...and readable", [r["phone"] for r in J.load(nested)] == ["9"])

    print("[4] synthesize() builds a row results.jsonl can hold")
    row = J.synthesize({"phone": "13800000001", "username": "u1", "password": "p1", "uid": "UID1"}, p)
    check("status ok", row["status"] == "ok", row)
    check("uid kept", row["uid"] == "UID1")
    check("via=journal", row["via"] == "journal")
    check("flagged", row["journal_recovered"] is True)
    check("no access token", "token" not in row)
    check("the note says it was free", "0 token" in row["note"], row.get("note"))
    check("source basename recorded", row["journal"] == os.path.basename(p))

    print("[5] recover() is free and idempotent")
    out = os.path.join(d, "out.jsonl")
    got, unresolved = J.recover(p, out)
    check("only the uid row is recoverable", [r["phone"] for r in got] == ["13800000001"],
          [r["phone"] for r in got])
    check("the uid-less one is reported", [u["phone"] for u in unresolved] == ["13800000002"],
          unresolved)
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"phone": "13800000001", "status": "ok"}) + "\n")
    check("already written -> nothing to do", J.recover(p, out)[0] == [])
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"phone": "13800000001", "status": "error"}) + "\n")
    check("any existing row blocks recovery (the runner decides, not the log)",
          J.recover(p, out)[0] == [])
    check("no journal at all -> empty", J.recover(os.path.join(d, "none"), out) == ([], []))

    print("[6] register_one journals the 303 before the caller can die")
    d2 = workdir("one")
    jp = os.path.join(d2, "r.jsonl.journal")
    res, cli, err = register(jp)
    check("no exception", err is None, err)
    check("row ok", (res or {}).get("status") == "ok", res)
    lines = J.load(jp)
    check("two lines", [r["phase"] for r in lines] == ["registered", "ok"], lines)
    check("uid recorded", lines[-1]["uid"] == "U1", lines[-1])
    check("the credentials are the ones in the row",
          lines[-1]["username"] == res["username"] and lines[-1]["password"] == res["password"],
          lines[-1])
    check("no token in the journal", "access_token" not in json.dumps(lines))

    print("[7] a failed registration writes nothing")
    d3 = workdir("fail")
    jp3 = os.path.join(d3, "r.jsonl.journal")
    res3, cli3, err3 = register(jp3, {"status": 400, "error": "验证码不正确", "location": None})
    check("it raises", err3 is not None)
    check("no journal file", not os.path.exists(jp3), os.path.exists(jp3))

    print("[8] vmax_go recovers for free, registers only the new number")
    import vmax_go
    d4 = workdir("go")
    out4 = os.path.join(d4, "results.jsonl")
    jp4 = out4 + ".journal"
    with open(jp4, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1, "phone": "13700000001", "phase": "registered",
                            "username": "old1", "password": "pw1"}) + "\n")
        f.write(json.dumps({"ts": 2, "phone": "13700000001", "phase": "ok",
                            "username": "old1", "password": "pw1", "uid": "OLDUID"}) + "\n")
    csv4 = os.path.join(d4, "cards.csv")
    write_csv(csv4, ["13700000001", "13700000002"])
    calls = []
    vmax_go.preflight = lambda *a, **k: []
    vmax_go.register_one = lambda ns: (calls.append(ns.phone) or
                                       {"phone": ns.phone, "status": "ok", "uid": "NEW",
                                        "ts": 3})
    rc = vmax_go.main(["--input", csv4, "--out", out4, "--yes", "--no-sms-check",
                       "--captcha", "param", "--captcha-param", "T"])
    rows4 = read_rows(out4)
    check("exit 0", rc == 0, rc)
    check("the recovered number was never submitted again", calls == ["13700000002"], calls)
    check("two rows on disk", len(rows4) == 2, rows4)
    rec = [r for r in rows4 if r["phone"] == "13700000001"]
    check("recovered row is ok", rec and rec[0]["status"] == "ok", rec)
    check("recovered uid came from the journal", rec and rec[0]["uid"] == "OLDUID", rec)
    check("recovered row says via=journal", rec and rec[0]["via"] == "journal", rec)
    summ4 = json.load(open(out4 + ".summary.json", encoding="utf-8"))
    check("summary counts it", summ4.get("journal_recovered") == 1, summ4.get("journal_recovered"))
    check("summary planned only the new number", summ4.get("planned") == 1, summ4.get("planned"))

    print("[9] vmax_batch recovers for free with two lanes")
    import vmax_batch
    d5 = workdir("batch")
    out5 = os.path.join(d5, "results.jsonl")
    jp5 = out5 + ".journal"
    for i, uid in ((1, "OLD1"), (2, "OLD2")):
        with open(jp5, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": i, "phone": f"1360000000{i}", "phase": "ok",
                                "username": f"u{i}", "password": f"p{i}", "uid": uid}) + "\n")
    csv5 = os.path.join(d5, "cards.csv")
    write_csv(csv5, ["13600000001", "13600000002", "13600000003"])
    calls5 = []
    vmax_batch.register_one = lambda ns: (calls5.append(ns.phone) or
                                          {"phone": ns.phone, "status": "ok", "uid": "NEW5",
                                           "ts": 3})
    argv = sys.argv
    sys.argv = ["vmax_batch.py", "--input", csv5, "--out", out5, "--workers", "2",
                "--delay", "0", "--no-sms-check", "--captcha", "param",
                "--captcha-param", "T"]
    try:
        rc5 = vmax_batch.main()
    finally:
        sys.argv = argv
    rows5 = read_rows(out5)
    check("exit 0", rc5 == 0, rc5)
    check("only the fresh number was submitted", calls5 == ["13600000003"], calls5)
    check("three rows", len(rows5) == 3, rows5)
    check("both recovered rows are ok", all(r["status"] == "ok" for r in rows5), rows5)
    check("both carry via=journal",
          sorted(r["phone"] for r in rows5 if r.get("via") == "journal")
          == ["13600000001", "13600000002"], rows5)

    print("[10] --no-journal: nothing written, nothing recovered")
    d6 = workdir("nojournal")
    out6 = os.path.join(d6, "results.jsonl")
    with open(out6 + ".journal", "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1, "phone": "13500000001", "phase": "ok",
                            "username": "u9", "password": "p9", "uid": "U9"}) + "\n")
    csv6 = os.path.join(d6, "cards.csv")
    write_csv(csv6, ["13500000001"])
    calls6 = []
    vmax_go.register_one = lambda ns: (calls6.append(ns.phone) or
                                       {"phone": ns.phone, "status": "ok", "uid": "X", "ts": 3})
    rc6 = vmax_go.main(["--input", csv6, "--out", out6, "--yes", "--no-sms-check",
                        "--captcha", "param", "--captcha-param", "T", "--no-journal"])
    rows6 = read_rows(out6)
    check("exit 0", rc6 == 0, rc6)
    check("the number was submitted normally", calls6 == ["13500000001"], calls6)
    check("one row, not a journal one", len(rows6) == 1 and not rows6[0].get("journal_recovered"),
          rows6)

    print("[11] recovering the whole pool is work, not 'nothing to do'")
    d7 = workdir("onlyrec")
    out7 = os.path.join(d7, "results.jsonl")
    with open(out7 + ".journal", "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1, "phone": "13400000001", "phase": "ok",
                            "username": "u7", "password": "p7", "uid": "U7"}) + "\n")
    csv7 = os.path.join(d7, "cards.csv")
    write_csv(csv7, ["13400000001"])
    calls7 = []
    vmax_go.register_one = lambda ns: (calls7.append(ns.phone) or {"status": "ok", "ts": 3})
    rc7 = vmax_go.main(["--input", csv7, "--out", out7, "--yes", "--no-sms-check",
                        "--captcha", "param", "--captcha-param", "T"])
    summ7 = json.load(open(out7 + ".summary.json", encoding="utf-8"))
    check("exit 0, not 4 (recovery is work)", rc7 == 0, rc7)
    check("nothing was registered again", calls7 == [], calls7)
    check("summary still says ok=0 (honest: nothing was registered)", summ7.get("ok") == 0,
          summ7.get("ok"))
    check("...and counts the recovery", summ7.get("journal_recovered") == 1, summ7)
    check("the recovered row is on disk", len(read_rows(out7)) == 1, read_rows(out7))

    print("[12] vmax_day re-verifies recovered rows (the ones we know least about)")
    import vmax_day
    check("ok>0 -> verify", vmax_day.should_verify({"ok": 2}) is True)
    check("recovered>0 alone -> verify",
          vmax_day.should_verify({"ok": 0, "journal_recovered": 1}) is True)
    check("nothing produced -> no verify",
          vmax_day.should_verify({"ok": 0, "journal_recovered": 0}) is False)
    check("no summary -> no verify", vmax_day.should_verify(None) is False)
    check("a corrupt summary does not raise",
          vmax_day.should_verify({"ok": "x", "journal_recovered": None}) is False)

    print("[13] vmax_fleet counts a recovered row like any other ok row")
    import vmax_fleet
    d8 = workdir("fleet")
    f8 = os.path.join(d8, "results.jsonl")
    with open(f8, "w", encoding="utf-8") as f:
        f.write(json.dumps({"phone": "13300000001", "status": "ok", "uid": "U8",
                            "username": "u8", "password": "p8", "via": "journal",
                            "journal_recovered": True}) + "\n")
        f.write(json.dumps({"phone": "13300000002", "status": "ok", "uid": "U9",
                            "username": "u9", "password": "p9"}) + "\n")
    led = vmax_fleet.build_ledger([f8])
    check("both are ok phones",
          sorted(led["ok_phones"]) == ["13300000001", "13300000002"], led["ok_phones"])
    check("no uid collision", led["uid_collisions"] == [], led["uid_collisions"])
    check("no duplicate registration", led["duplicate_ok"] == [], led["duplicate_ok"])
    check("totals count both", led["totals"]["ok"] == 2, led["totals"])

    for d in TMP:
        shutil.rmtree(d, ignore_errors=True)
    print("ALL JOURNAL TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
