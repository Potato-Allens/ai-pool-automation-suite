# -*- coding: utf-8 -*-
"""End-to-end test: run vmax_day.py as a real subprocess against the local mock auth
server (tests/mock_e2e.py).  No network, no captcha, no SMS.

Why this exists: tests/test_day.py fakes `_execute`, so it proves the orchestration
logic but not the **command lines**.  A wrong flag name between the wrapper and
vmax_go / vmax_login / vmax_report would only show up on the day the cards arrive.
Here the real CLIs are spawned: register -> verify -> report -> manifest.

    [1] one card, full pipeline            [2] 429 on the second write
    [3] pool of taken cards                [4] mixed pool, skip mode
    [5] mixed pool, --on-taken reset       [6] reset channel capped (daily limit)
    [7] the same results file the next day: the capped numbers are rescued
    [9] a dead SMS channel stops vmax_go before the first token
   [10] ... and stops vmax_batch, which has no preflight of its own
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import mock_e2e as M  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def reset_mock():
    M.Cfg.cooldown = 0
    M.Cfg.reject_first = 0
    M.Cfg.omit_code = False
    M.Cfg.codes.clear()
    M.Cfg.users.clear()
    M.Cfg.attempts.clear()
    M.Cfg.last_send.clear()
    M.Cfg.oauth.clear()
    M.Cfg.phone_taken.clear()
    M.Cfg.send_code_fail.clear()
    M.Cfg.rate_limit_after = 0
    M.Cfg.counter = 0
    M.Cfg.sends = 0
    M.Cfg.register_tries = 0
    M.Cfg.login_tries = 0
    M.Cfg.reset_rate_limited = False
    M.Cfg.reset_reject_first = 0


def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_day(td, phones, sms_dir, out_name, extra=(), timeout=120):
    cards = os.path.join(td, out_name + ".csv")
    with open(cards, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone", "password"])
        for p in phones:
            w.writerow([p, "pw-" + p[-4:]])
    run_dir = os.path.join(td, out_name)
    out = os.path.join(run_dir, "results.jsonl")
    env = dict(os.environ, VMAX_PACE_STATE=os.path.join(td, "pace_" + out_name + ".json"),
               VMAX_RESEND_WAIT="0", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-u", os.path.join(ROOT, "vmax_day.py"),
           "--input", cards, "--out", out,
           "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback",
           "--captcha", "param", "--captcha-param", M.Cfg.token,
           "--skip-farm", "--skip-preflight",
           "--sms", "file:local", "--sms-dir", sms_dir,
           "--sms-timeout", "15", "--yes"] + list(extra)
    t0 = __import__("time").time()
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env, timeout=timeout)
    return cp, run_dir, out, cards, round(__import__("time").time() - t0, 1)


def start_sync(sms_dir):
    stop = threading.Event()

    def loop():
        while not stop.wait(0.1):
            for ph, code in list(M.Cfg.codes.items()):
                try:
                    with open(os.path.join(sms_dir, ph + ".txt"), "w", encoding="utf-8") as f:
                        f.write(code)
                except OSError:
                    pass

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return stop, th


def main():
    with tempfile.TemporaryDirectory() as td:
        sms_dir = os.path.join(td, "sms")
        os.makedirs(sms_dir)
        M.Cfg.codes_path = os.path.join(td, "codes.jsonl")
        with M.Server():
            stop, th = start_sync(sms_dir)
            try:
                print("[1] one card, full pipeline through the real CLIs")
                reset_mock()
                phone = "13700137001"
                cp, run_dir, out, cards, secs = run_day(td, [phone], sms_dir, "run1")
                man = json.load(open(os.path.join(run_dir, "manifest.json"), encoding="utf-8"))
                if not os.path.exists(out):
                    print("--- child stdout ---")
                    print(cp.stdout[-3000:])
                    print("--- child stderr ---")
                    print(cp.stderr[-3000:])
                rows = read_jsonl(out) if os.path.exists(out) else []
                logins = read_jsonl(os.path.join(run_dir, "logins.jsonl"))
                check("exit 0", cp.returncode == 0, cp.returncode)
                check("registered", rows and rows[0]["status"] == "ok", rows[:1])
                check("uid present", bool(rows and rows[0].get("uid")), rows[:1])
                check("re-checked by password", logins and logins[0]["status"] == "ok", logins[:1])
                check("verdict ok", man["verdict"] == "ok", man.get("verdict"))
                check("steps ran", [s["name"] for s in man["steps"]]
                      == ["batch", "verify", "report", "report_login"],
                      [s["name"] for s in man["steps"]])
                check("input hash matches",
                      man["input"]["sha256"] == hashlib.sha256(open(cards, "rb").read()).hexdigest())
                check("farm not started", man.get("farm") == {} or "starts" not in man.get("farm", {}),
                      man.get("farm"))
                check("report text attached",
                      bool(man.get("report", {}).get("logins_text")), man.get("report"))
                check("finished in a sane time", secs < 60, secs)
                if cp.returncode != 0:
                    print(cp.stdout[-2000:])
                    print(cp.stderr[-2000:])

                print("[2] 429 on the second write -> exit 3, first account still verified")
                reset_mock()
                M.Cfg.rate_limit_after = 1
                cp2, run2, out2, _, _ = run_day(td, ["13700137002", "13700137003"], sms_dir, "run2")
                if cp2.returncode != 3:
                    print("rows:", json.dumps(read_jsonl(out2), ensure_ascii=False)[:1500])
                man2 = json.load(open(os.path.join(run2, "manifest.json"), encoding="utf-8"))
                check("exit 3 (aborted on 429)", cp2.returncode == 3, cp2.returncode)
                check("verdict aborted_429", man2["verdict"] == "aborted_429", man2.get("verdict"))
                check("summary says aborted", man2["batch_summary"]["aborted_on_429"] is True,
                      man2.get("batch_summary"))
                check("exactly one ok row", man2["batch_summary"]["ok"] == 1,
                      man2.get("batch_summary"))
                check("verify still ran for the ok row",
                      "verify" in [s["name"] for s in man2["steps"]],
                      [s["name"] for s in man2["steps"]])
                row429 = [r for r in read_jsonl(out2) if r.get("phone") == "13700137003"]
                check("429 row keeps its bucket",
                      bool(row429) and row429[0].get("register_error_kind") == "rate_limited",
                      row429[:1])
                check("429 row keeps the HTTP status",
                      bool(row429) and row429[0].get("register_http_status") == 429, row429[:1])
                check("429 visible in the error text",
                      bool(row429) and "429" in str(row429[0].get("error")), row429[:1])

                print("[3] nothing to do (every card already registered) -> exit 7")
                reset_mock()
                M.Cfg.phone_taken = {"13700137004"}
                cp3, run3, out3, _, _ = run_day(td, ["13700137004"], sms_dir, "run3")
                if cp3.returncode != 7:
                    print("rows:", json.dumps(read_jsonl(out3), ensure_ascii=False)[:1500])
                man3 = json.load(open(os.path.join(run3, "manifest.json"), encoding="utf-8"))
                check("exit 7 (pool had taken)", cp3.returncode == 7, cp3.returncode)
                check("verdict pool_had_taken", man3["verdict"] == "pool_had_taken",
                      man3.get("verdict"))
                check("no verify for a taken-only run",
                      "verify" not in [s["name"] for s in man3["steps"]],
                      [s["name"] for s in man3["steps"]])
                rows3 = read_jsonl(out3)
                check("taken row keeps its bucket",
                      bool(rows3) and rows3[0].get("register_error_kind") == "phone_taken",
                      rows3[:1])
                check("taken row keeps the send-code answer",
                      bool(rows3) and (rows3[0].get("send_code") or {}).get("ok") is False,
                      rows3[:1])
                check("no SMS spent on a taken number", M.Cfg.sends == 0, M.Cfg.sends)

                print("[4] mixed pool (1 fresh + 1 taken) -> exit 7, fresh card still verified")
                reset_mock()
                M.Cfg.phone_taken = {"13700137006"}
                cp4, run4, out4, _, _ = run_day(td, ["13700137005", "13700137006"],
                                                sms_dir, "run4")
                if cp4.returncode != 7:
                    print("rows:", json.dumps(read_jsonl(out4), ensure_ascii=False)[:1500])
                man4 = json.load(open(os.path.join(run4, "manifest.json"), encoding="utf-8"))
                bs4 = man4.get("batch_summary") or {}
                rows4 = {r["phone"]: r for r in read_jsonl(out4)}
                check("exit 7 (pool had taken)", cp4.returncode == 7, cp4.returncode)
                check("one registered", bs4.get("ok") == 1, bs4)
                check("one skipped as taken", bs4.get("taken") == 1, bs4)
                check("fresh card ok", rows4.get("13700137005", {}).get("status") == "ok",
                      rows4.get("13700137005"))
                check("taken card bucketed", rows4.get("13700137006", {}).get(
                    "register_error_kind") == "phone_taken", rows4.get("13700137006"))
                logins4 = read_jsonl(os.path.join(run4, "logins.jsonl"))
                check("verify covers the fresh card only",
                      [r.get("phone") for r in logins4] == ["13700137005"], logins4[:2])
                check("still exactly one SMS for the whole pool", M.Cfg.sends == 1, M.Cfg.sends)
                print("[5] mixed pool + --on-taken reset -> exit 0, taken card recovered")
                reset_mock()
                taken5 = "13700137008"
                M.Cfg.users[taken5] = {"username": "prevowner", "password": "OldPass12345",
                                       "uid": "prev5"}
                M.Cfg.phone_taken = {taken5}
                cp5, run5, out5, _, _ = run_day(td, ["13700137007", taken5], sms_dir, "run5",
                                                extra=("--on-taken", "reset"))
                if cp5.returncode != 0:
                    print("rows:", json.dumps(read_jsonl(out5), ensure_ascii=False)[:1500])
                    print(cp5.stdout[-1500:])
                man5 = json.load(open(os.path.join(run5, "manifest.json"), encoding="utf-8"))
                bs5 = man5.get("batch_summary") or {}
                rows5 = read_jsonl(out5)
                check("exit 0", cp5.returncode == 0, cp5.returncode)
                check("manifest records on_taken=reset",
                      (man5.get("config") or {}).get("on_taken") == "reset",
                      man5.get("config"))
                check("verdict ok", man5["verdict"] == "ok", man5.get("verdict"))
                check("batch summary ok=2", bs5.get("ok") == 2, bs5)
                check("batch summary reused=1", bs5.get("reused") == 1, bs5)
                check("batch summary taken=0", bs5.get("taken") == 0, bs5)
                reset_rows5 = [r for r in rows5 if r.get("via") == "reset"]
                check("one reset row", len(reset_rows5) == 1, reset_rows5[:1])
                check("reset row is ok and has a uid",
                      bool(reset_rows5) and reset_rows5[0].get("status") == "ok"
                      and bool(reset_rows5[0].get("uid")), reset_rows5[:1])
                logins5 = read_jsonl(os.path.join(run5, "logins.jsonl"))
                check("verify covers both accounts",
                      sorted(r.get("phone") for r in logins5)
                      == sorted(["13700137007", taken5]), logins5)
                check("recovered account logs in with the new password",
                      bool(logins5) and all(r.get("status") == "ok" for r in logins5), logins5)
                check("two SMS (one reset, one registration)", M.Cfg.sends == 2, M.Cfg.sends)

                print("[6] reset channel capped (2 numbers) -> exit 7, verdict names it")
                reset_mock()
                taken6 = ["13700137010", "13700137011"]
                for k, ph in enumerate(taken6):
                    M.Cfg.users[ph] = {"username": "prev6_%d" % k,
                                       "password": "OldPass12345", "uid": "prev6%d" % k}
                M.Cfg.phone_taken = set(taken6)
                M.Cfg.reset_rate_limited = True
                cp6, run6, out6, _, _ = run_day(td, ["13700137009"] + taken6, sms_dir, "run6",
                                                extra=("--on-taken", "reset"))
                man6 = json.load(open(os.path.join(run6, "manifest.json"), encoding="utf-8"))
                bs6 = man6.get("batch_summary") or {}
                rows6 = read_jsonl(out6)
                fresh6 = [r for r in rows6 if r.get("phone") == "13700137009"]
                check("exit 7", cp6.returncode == 7, cp6.returncode)
                check("verdict names the capped reset channel",
                      man6["verdict"] == "pool_had_taken_reset_capped", man6.get("verdict"))
                check("summary says reset_limited", bs6.get("reset_limited") is True, bs6)
                check("fresh card still registered",
                      bool(fresh6) and fresh6[0].get("status") == "ok", fresh6[:1])
                check("both taken cards bucketed as daily_limit",
                      len([r for r in rows6 if r.get("reset_error_kind") == "daily_limit"]) == 2,
                      [r.get("reset_error_kind") for r in rows6])
                check("only one SMS (the fresh card)", M.Cfg.sends == 1, M.Cfg.sends)
                logins6 = read_jsonl(os.path.join(run6, "logins.jsonl"))
                check("verify covers the fresh card only",
                      [r.get("phone") for r in logins6] == ["13700137009"], logins6[:2])

                print("[7] same results file, next day: the capped numbers are rescued")
                # vmax_day derives its run dir from --out, so each simulated day gets
                # its own dir with a copy of yesterday's results file (what a real
                # operator does with runs\1026, runs\1027, ...).
                def copy_rows(src_path, dst_path, shift=0):
                    rows = read_jsonl(src_path)
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    with open(dst_path, "w", encoding="utf-8") as f:
                        for r in rows:
                            if shift and r.get("ts"):
                                r["ts"] = int(r["ts"]) - shift
                            f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    return len(rows)

                # 7a -- same day again.  The memo says "today's quota is gone", so the
                # run must spend nothing and still name the cap in the verdict.
                day_a = os.path.join(td, "run7a", "results.jsonl")
                n7 = copy_rows(out6, day_a)
                sends7 = M.Cfg.sends
                cp7a, run7a, out7a, _, _ = run_day(td, ["13700137009"] + taken6, sms_dir,
                                                   "run7a", extra=("--on-taken", "reset"))
                man7a = json.load(open(os.path.join(run7a, "manifest.json"), encoding="utf-8"))
                bs7a = man7a.get("batch_summary") or {}
                check("7a exit 7 (two taken rows still wait)", cp7a.returncode == 7,
                      (cp7a.returncode, cp7a.stdout[-400:]))
                check("7a verdict still names the capped channel",
                      man7a["verdict"] == "pool_had_taken_reset_capped", man7a.get("verdict"))
                check("7a summary says nothing_to_do", bs7a.get("nothing_to_do") is True, bs7a)
                check("7a skipped_capped=2", bs7a.get("skipped_capped") == 2, bs7a)
                check("7a spent no SMS", M.Cfg.sends == sends7, M.Cfg.sends - sends7)
                check("7a wrote no rows", len(read_jsonl(out7a)) == n7,
                      len(read_jsonl(out7a)))

                # 7b -- pretend it is tomorrow: every row is re-dated, the quota is back.
                M.Cfg.reset_rate_limited = False
                day_b = os.path.join(td, "run7b", "results.jsonl")
                copy_rows(out6, day_b, shift=86400)
                sends7b = M.Cfg.sends
                cp7b, run7b, out7b, _, _ = run_day(td, ["13700137009"] + taken6, sms_dir,
                                                   "run7b", extra=("--on-taken", "reset"))
                man7b = json.load(open(os.path.join(run7b, "manifest.json"), encoding="utf-8"))
                bs7b = man7b.get("batch_summary") or {}
                rows7b = read_jsonl(out7b)
                rescued = [r for r in rows7b if r.get("via") == "reset"]
                logins7 = read_jsonl(os.path.join(run7b, "logins.jsonl"))
                if cp7b.returncode != 0:
                    print(cp7b.stdout[-2500:])
                    print(cp7b.stderr[-1500:])
                check("7b exit 0 (both rescued)", cp7b.returncode == 0, cp7b.returncode)
                check("7b verdict ok", man7b["verdict"] == "ok", man7b.get("verdict"))
                check("7b reused=2", bs7b.get("reused") == 2, bs7b)
                check("7b skipped_capped=0", bs7b.get("skipped_capped") == 0, bs7b)
                check("7b two reset rows, both ok",
                      sorted(r.get("phone") for r in rescued if r.get("status") == "ok")
                      == sorted(taken6),
                      [(r.get("phone"), r.get("status")) for r in rescued])
                check("7b no reset channel closed", bs7b.get("reset_limited") is False, bs7b)
                check("7b two SMS (one per rescue)", M.Cfg.sends - sends7b == 2,
                      M.Cfg.sends - sends7b)
                check("7b verify covers all three accounts",
                      sorted(r.get("phone") for r in logins7)
                      == sorted(["13700137009"] + taken6), logins7)
                check("7b every login ok",
                      bool(logins7) and all(r.get("status") == "ok" for r in logins7),
                      logins7)

                print("[8] a dead SMS channel stops the day before any token/SMS spend")
                sends8 = M.Cfg.sends
                cp8, run8, out8, _, _ = run_day(
                    td, ["13700137010"], sms_dir, "run8",
                    extra=("--sms", "http:http://127.0.0.1:1/code?phone={phone}"))
                man8 = json.load(open(os.path.join(run8, "manifest.json"), encoding="utf-8"))
                check("8 exit 9", cp8.returncode == 9,
                      (cp8.returncode, cp8.stderr[-300:]))
                check("8 verdict sms_channel_down",
                      man8["verdict"] == "sms_channel_down", man8.get("verdict"))
                check("8 probe recorded as failed",
                      (man8.get("sms_check") or {}).get("ok") is False, man8.get("sms_check"))
                check("8 no result rows written", not os.path.exists(out8), out8)
                check("8 no SMS sent", M.Cfg.sends == sends8, M.Cfg.sends - sends8)
                check("8 no step ran", man8["steps"] == [], man8["steps"])

                print("[9] a dead SMS channel stops vmax_go before any token/SMS spend")
                reset_mock()
                tries9, sends9 = M.Cfg.register_tries, M.Cfg.sends
                cards9 = os.path.join(td, "run9.csv")
                with open(cards9, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["phone", "password"])
                    w.writerow(["13700137011", "pw-7011"])
                out9 = os.path.join(td, "run9", "results.jsonl")
                env9 = dict(os.environ, VMAX_NOPACE="1", VMAX_RESEND_WAIT="0",
                            VMAX_PACE_STATE=os.path.join(td, "pace_run9.json"),
                            PYTHONIOENCODING="utf-8")
                cp9 = subprocess.run(
                    [sys.executable, "-u", os.path.join(ROOT, "vmax_go.py"),
                     "--input", cards9, "--out", out9,
                     "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback",
                     "--captcha", "param", "--captcha-param", M.Cfg.token,
                     "--sms", "http:http://127.0.0.1:1/code?phone={phone}", "--yes"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", env=env9, timeout=120)
                check("9 go exit 9", cp9.returncode == 9,
                      (cp9.returncode, cp9.stdout[-500:], cp9.stderr[-300:]))
                check("9 go printed the gate",
                      "[go] sms channel: DOWN" in cp9.stdout, cp9.stdout[-500:])
                check("9 go wrote no results row", not os.path.exists(out9), out9)
                check("9 go spent no register write",
                      M.Cfg.register_tries == tries9, M.Cfg.register_tries - tries9)
                check("9 go sent no SMS", M.Cfg.sends == sends9, M.Cfg.sends - sends9)

                print("[10] the same gate protects a direct vmax_batch run")
                tries10, sends10 = M.Cfg.register_tries, M.Cfg.sends
                out10 = os.path.join(td, "run10", "results.jsonl")
                env10 = dict(os.environ, VMAX_NOPACE="1", VMAX_RESEND_WAIT="0",
                             VMAX_PACE_STATE=os.path.join(td, "pace_run10.json"),
                             PYTHONIOENCODING="utf-8")
                cp10 = subprocess.run(
                    [sys.executable, "-u", os.path.join(ROOT, "vmax_batch.py"),
                     "--input", cards9, "--out", out10,
                     "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback",
                     "--captcha", "param", "--captcha-param", M.Cfg.token,
                     "--sms", "http:http://127.0.0.1:1/code?phone={phone}"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", env=env10, timeout=120)
                check("10 batch exit 9", cp10.returncode == 9,
                      (cp10.returncode, cp10.stdout[-500:], cp10.stderr[-300:]))
                check("10 batch wrote no results row", not os.path.exists(out10), out10)
                check("10 batch spent no register write",
                      M.Cfg.register_tries == tries10, M.Cfg.register_tries - tries10)
                check("10 batch sent no SMS", M.Cfg.sends == sends10, M.Cfg.sends - sends10)

                print("[11] --form-captcha device: the /register submit needs no token")
                reset_mock()
                M.Cfg.codes.clear()
                rowsD_len = 0
                cp11, run11, out11, _, _ = run_day(td, ["13700137012"], sms_dir, "run11",
                                                   extra=("--form-captcha", "device"))
                rows11 = read_jsonl(out11) if os.path.exists(out11) else []
                if cp11.returncode != 0:
                    print("--- child stdout ---")
                    print(cp11.stdout[-2500:])
                check("11 exit 0", cp11.returncode == 0,
                      (cp11.returncode, cp11.stdout[-800:]))
                check("11 registered", bool(rows11) and rows11[0].get("status") == "ok",
                      rows11[:1])
                check("11 register 303 and a code",
                      bool(rows11) and (rows11[0].get("register") or {}).get("status") == 303,
                      (rows11[:1] and rows11[0].get("register")))
                check("11 the row says device", bool(rows11)
                      and rows11[0].get("form_captcha") == "device",
                      rows11[:1] and rows11[0].get("form_captcha"))
                check("11 signup-check said no captcha",
                      bool(rows11) and (rows11[0].get("device") or {}).get("ok") is True,
                      rows11[:1] and rows11[0].get("device"))
                check("11 exactly one SMS went out (send-code still pays)",
                      M.Cfg.sends == 1, M.Cfg.sends)
                check("11 exactly one register write", M.Cfg.register_tries == 1,
                      M.Cfg.register_tries)

            finally:
                stop.set()
                th.join(timeout=2)

    print("ALL DAY E2E TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
