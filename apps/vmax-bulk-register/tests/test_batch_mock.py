# -*- coding: utf-8 -*-
"""Offline proof of the batch orchestrator: vmax_go on 3 accounts + resume.

The only fake parts are the auth server (tests/mock_e2e.py) and the phone
forwarder thread; everything else is the production path -- the same
vmax_go -> run_one -> register_one -> vmax_sms.HttpSms -> vmax_smsd chain.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import requests  # noqa: E402

import mock_e2e as M  # noqa: E402

PY = sys.executable
SMSD = os.path.join(ROOT, "vmax_smsd.py")
SECRET = "<TEST_SECRET>-batch"
PHONES = ["13500135001", "13500135002", "13500135003"]

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def forwarder(sms_dir, daemon, stop):
    seen = {}
    while not stop.is_set():
        try:
            for name in os.listdir(sms_dir):
                if not name.endswith(".txt"):
                    continue
                path = os.path.join(sms_dir, name)
                mtime = os.path.getmtime(path)
                if seen.get(path) == mtime:
                    continue
                seen[path] = mtime
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                requests.post(daemon + "/ingest", json={"phone": name[:-4], "text": text},
                              headers={"X-Sms-Secret": SECRET}, timeout=5)
        except Exception:
            pass
        stop.wait(0.25)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="batch_mock_")
    sms_dir = os.path.join(tmp, "sms")
    os.makedirs(sms_dir, exist_ok=True)

    M.Cfg.cooldown = 0
    M.Cfg.reject_first = 0
    M.Cfg.omit_code = False
    M.Cfg.codes.clear()
    M.Cfg.attempts.clear()
    M.Cfg.last_send.clear()
    M.Cfg.sms_dir = sms_dir
    M.Cfg.codes_path = os.path.join(tmp, "codes.jsonl")

    dport = free_port()
    daemon = f"http://127.0.0.1:{dport}"
    d = subprocess.Popen([PY, SMSD, "--host", "127.0.0.1", "--port", str(dport),
                          "--secret", SECRET, "--state", os.path.join(tmp, "inbox.jsonl")],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = threading.Event()
    th = None
    try:
        for _ in range(60):
            try:
                requests.get(daemon + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.15)

        with M.Server() as srv:
            base = f"http://127.0.0.1:{srv.port}"
            th = threading.Thread(target=forwarder, args=(sms_dir, daemon, stop), daemon=True)
            th.start()

            csv = os.path.join(tmp, "accounts.csv")
            with open(csv, "w", encoding="utf-8") as f:
                f.write("phone\n" + "\n".join(PHONES + [PHONES[0]]) + "\n")
            out = os.path.join(tmp, "results.jsonl")
            env = dict(os.environ, VMAX_NOPACE="1", VMAX_RESEND_WAIT="0")
            common = [PY, "vmax_go.py", "--input", csv, "--out", out,
                      "--base", base, "--redirect", base + "/auth/callback",
                      "--captcha", "param", "--captcha-param", M.Cfg.token,
                      "--form-captcha", "reuse", "--sms-resends", "0",
                      "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}&timeout=10&since={{since}}",
                      "--force", "--yes", "--delay", "0"]

            print("[1] vmax_go registers 3 accounts through the real SMS inbox")
            r = subprocess.run(common, cwd=ROOT, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            tail = "\n".join(r.stdout.strip().splitlines()[-4:])
            print(tail)
            check("exit 0", r.returncode == 0, f"{r.returncode} {r.stderr[-200:]}")
            check("the SMS gate saw a live channel",
                  "[go] sms channel: OK -- health 200" in r.stdout, r.stdout[-500:])
            recs = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
            check("3 records", len(recs) == 3, len(recs))
            check("all ok", all(x.get("status") == "ok" for x in recs),
                  [(x.get("phone"), x.get("status"), str(x.get("error"))[:40]) for x in recs])
            check("all have uid", all(x.get("uid") for x in recs), [x.get("uid") for x in recs])
            check("3 register writes", M.Cfg.register_tries == 3, M.Cfg.register_tries)

            print("[2] summary file carries the counters")
            summ = out + ".summary.json"
            s = json.load(open(summ, encoding="utf-8")) if os.path.exists(summ) else {}
            check("summary ok=3", s.get("ok") == 3, s)
            check("summary fail=0", s.get("fail") == 0, s)
            check("duplicate row collapsed", s.get("duplicates_collapsed") == 1,
                  s.get("duplicates_collapsed"))
            check("summary records the form policy", s.get("form_captcha") == "reuse", s)
            check("summary: 0 device rides under reuse", s.get("form_device") == 0, s)
            check("summary: 0 extra form tokens under reuse",
                  s.get("form_extra_tokens") == 0, s.get("form_extra_tokens"))
            check("every reuse row says so (no silent 'none')",
                  [x.get("form_captcha") for x in recs] == ["reuse"] * 3,
                  [x.get("form_captcha") for x in recs])
            check("summary: 3 reuse rides", s.get("form_reuse") == 3, s.get("form_reuse"))
            check("summary: form_rows has no unknown bucket",
                  "none" not in (s.get("form_rows") or {}), s.get("form_rows"))
            check("summary: device + reuse + extra == rows",
                  s.get("form_device", 0) + s.get("form_reuse", 0)
                  + s.get("form_extra_tokens", 0) == 3, s)

            print("[3] re-running the same --out resumes and does nothing")
            r2 = subprocess.run(common, cwd=ROOT, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            check("exit 4 (nothing to do)", r2.returncode == 4, r2.returncode)
            check("no extra writes", M.Cfg.register_tries == 3, M.Cfg.register_tries)
            check("said nothing to do", "nothing to do" in r2.stdout, r2.stdout[-200:])
    finally:
        stop.set()
        if th:
            th.join(timeout=2)
        # NOTE: the daemon must stay alive for sections [4]-[7]; it is stopped at
        # the very end. Killing it here made later SMS waits hang for sms_timeout.

    with M.Server() as srv:
        base = f"http://127.0.0.1:{srv.port}"
        stop2 = threading.Event()
        th2 = threading.Thread(target=forwarder, args=(sms_dir, daemon, stop2), daemon=True)
        th2.start()
        try:
            print("[4] --max-fail 1 stops after the first bad number")
            csv2 = os.path.join(tmp, "mixed.csv")
            with open(csv2, "w", encoding="utf-8") as f:
                f.write("phone\n17000000001\n" + "\n".join(PHONES) + "\n")
            out2 = os.path.join(tmp, "mixed.jsonl")
            env = dict(os.environ, VMAX_NOPACE="1", VMAX_RESEND_WAIT="0")
            common2 = [PY, "vmax_go.py", "--input", csv2, "--out", out2,
                       "--base", base, "--redirect", base + "/auth/callback",
                       "--captcha", "param", "--captcha-param", M.Cfg.token,
                       "--form-captcha", "reuse", "--sms-resends", "0",
                       "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}&timeout=10&since={{since}}",
                       "--force", "--yes", "--delay", "0", "--max-fail", "1"]
            before = M.Cfg.register_tries
            r3 = subprocess.run(common2, cwd=ROOT, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            check("exit 5", r3.returncode == 5, f"{r3.returncode} {r3.stdout[-200:]}")
            recs2 = [json.loads(l) for l in open(out2, encoding="utf-8") if l.strip()]
            check("only the bad number ran", len(recs2) == 1, len(recs2))
            check("it was the virtual range", recs2[0].get("phone") == "17000000001",
                  recs2[0].get("phone"))
            check("no register write burned", M.Cfg.register_tries == before, M.Cfg.register_tries)
            s2 = json.load(open(out2 + ".summary.json", encoding="utf-8"))
            check("summary flags the stop", s2.get("stopped_on_max_fail") is True, s2)

            print("[5] --max-minutes already elapsed stops before the first row")
            out3 = os.path.join(tmp, "minutes.jsonl")
            common3 = common2[:-2] + ["--max-minutes", "0.000001"]
            r4 = subprocess.run(common3, cwd=ROOT, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            check("exit 6", r4.returncode == 6, f"{r4.returncode} {r4.stdout[-200:]}")
            recs3 = [json.loads(l) for l in open(out3, encoding="utf-8") if l.strip()] if os.path.exists(out3) else []
            check("no row started", len(recs3) == 0, len(recs3))

            print("[6] vmax_batch (direct path) honours dedupe + --max-fail")
            csv4 = os.path.join(tmp, "dup.csv")
            with open(csv4, "w", encoding="utf-8") as f:
                f.write("phone\n17000000001\n" + "\n".join(PHONES + [PHONES[1]]) + "\n")
            out4 = os.path.join(tmp, "dup.jsonl")
            cmd4 = [PY, "vmax_batch.py", "--input", csv4, "--out", out4,
                    "--base", base, "--redirect", base + "/auth/callback",
                    "--captcha", "param", "--captcha-param", M.Cfg.token,
                    "--form-captcha", "reuse", "--sms-resends", "0",
                    "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}&timeout=10&since={{since}}",
                    "--delay", "0", "--max-fail", "1"]
            before4 = M.Cfg.register_tries
            r5 = subprocess.run(cmd4, cwd=ROOT, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
            check("batch exit 5", r5.returncode == 5, f"{r5.returncode} {r5.stdout[-200:]}")
            recs4 = [json.loads(l) for l in open(out4, encoding="utf-8") if l.strip()]
            check("batch stopped after 1", len(recs4) == 1, len(recs4))
            check("batch dedupe announced", "collapsed 1 duplicate" in r5.stdout, r5.stdout[-200:])
            check("batch burned no register write", M.Cfg.register_tries == before4, M.Cfg.register_tries)
            print("[7] 429 aborts the parallel batch instead of grinding through it")
            # fresh numbers so earlier sections cannot mask the accounting
            six = ["13500135005", "13500135006", "13500135007",
                   "13500135008", "13500135009", "13500135010"]
            csv5 = os.path.join(tmp, "par429.csv")
            with open(csv5, "w", encoding="utf-8") as f:
                f.write("phone\n" + "\n".join(six) + "\n")
            out5 = os.path.join(tmp, "par429.jsonl")
            M.Cfg.register_tries = 0
            M.Cfg.rate_limit_after = 2
            cmd5 = [PY, "vmax_batch.py", "--input", csv5, "--out", out5,
                    "--workers", "2", "--delay", "0",
                    "--base", base, "--redirect", base + "/auth/callback",
                    "--captcha", "param", "--captcha-param", M.Cfg.token,
                    "--form-captcha", "reuse", "--sms-resends", "0",
                    "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}&timeout=10&since={{since}}"]
            try:
                r6 = subprocess.run(cmd5, cwd=ROOT, env=env, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=120)
            finally:
                M.Cfg.rate_limit_after = 0
            print("\n".join(r6.stdout.strip().splitlines()[-3:]))
            check("batch exit 3 (aborted on 429)", r6.returncode == 3,
                  f"{r6.returncode} {r6.stdout[-300:]}")
            recs5 = [json.loads(l) for l in open(out5, encoding="utf-8") if l.strip()] \
                if os.path.exists(out5) else []
            check("did not attempt all six", len(recs5) < 6, len(recs5))
            check("stopped within workers*2 rows", len(recs5) <= 4, len(recs5))
            check("at least one account landed", any(x.get("status") == "ok" for x in recs5),
                  [(x.get("phone"), x.get("status")) for x in recs5])
            check("said it is stopping",
                  "then stopping" in r6.stdout or "not attempted" in r6.stdout,
                  r6.stdout[-300:])

            print("[8] --form-captcha device: 1 token per card, and the summary proves it")
            dev = ["13500135011", "13500135012"]
            csv6 = os.path.join(tmp, "device.csv")
            with open(csv6, "w", encoding="utf-8") as f:
                f.write("phone\n" + "\n".join(dev) + "\n")
            out6 = os.path.join(tmp, "device.jsonl")
            sends_before = M.Cfg.sends
            writes_before = M.Cfg.register_tries
            cmd6 = [PY, "vmax_go.py", "--input", csv6, "--out", out6,
                    "--base", base, "--redirect", base + "/auth/callback",
                    "--captcha", "param", "--captcha-param", M.Cfg.token,
                    "--form-captcha", "device", "--sms-resends", "0",
                    "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}&timeout=10&since={{since}}",
                    "--force", "--yes", "--delay", "0"]
            r7 = subprocess.run(cmd6, cwd=ROOT, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120)
            print("\n".join(r7.stdout.strip().splitlines()[-3:]))
            recs6 = [json.loads(l) for l in open(out6, encoding="utf-8") if l.strip()] \
                if os.path.exists(out6) else []
            check("device exit 0", r7.returncode == 0, f"{r7.returncode} {r7.stdout[-300:]}")
            check("both rows rode the device path",
                  [x.get("form_captcha") for x in recs6] == ["device", "device"],
                  [x.get("form_captcha") for x in recs6])
            check("each row carries device.ok with requires_captcha=false",
                  all(((x.get("device") or {}).get("ok") is True
                       and ((x.get("device") or {}).get("signup_check") or {})
                       .get("requires_captcha") is False) for x in recs6),
                  [x.get("device") for x in recs6])
            check("one SMS per card and nothing more",
                  M.Cfg.sends - sends_before == 2, M.Cfg.sends - sends_before)
            check("one /register write per card",
                  M.Cfg.register_tries - writes_before == 2,
                  M.Cfg.register_tries - writes_before)
            s6 = json.load(open(out6 + ".summary.json", encoding="utf-8")) \
                if os.path.exists(out6 + ".summary.json") else {}
            check("summary: policy device", s6.get("form_captcha") == "device", s6)
            check("summary: two device rides", s6.get("form_device") == 2, s6)
            check("summary: zero fallback and zero extra tokens",
                  s6.get("form_fallback") == 0 and s6.get("form_extra_tokens") == 0, s6)

        finally:
            stop2.set()
            th2.join(timeout=2)
            d.terminate()
            try:
                d.wait(timeout=5)
            except Exception:
                d.kill()

    print("ALL BATCH-MOCK TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
