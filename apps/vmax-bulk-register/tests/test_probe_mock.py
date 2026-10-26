# -*- coding: utf-8 -*-
"""Vet a number pool cheaply: one SMS each, no account, no /register write.

Proves the probe tool end to end against the mock auth server + a real vmax_smsd:
  * a good number -> status=ok, SMS seconds recorded
  * a number the server refuses -> status=send_rejected (no SMS waited)
  * POST /register is never called  -> Cfg.register_tries == 0
  * the code itself never lands in probe.jsonl (only length + mask)
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
SECRET = "<TEST_SECRET>-probe"

GOOD = ["13500135021", "13500135022"]
BAD = "13500135023"
PHONES = GOOD + [BAD]

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
        stop.wait(0.2)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="probe_")
    sms_dir = os.path.join(tmp, "sms")
    tok_dir = os.path.join(tmp, "tokens")
    os.makedirs(sms_dir, exist_ok=True)
    os.makedirs(tok_dir, exist_ok=True)
    for i in range(5):
        path = os.path.join(tok_dir, f"tok_{i:04d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"param": M.Cfg.token}, f)
        os.utime(path, (time.time() + i, time.time() + i))

    M.Cfg.cooldown = 0
    M.Cfg.codes.clear()
    M.Cfg.attempts.clear()
    M.Cfg.last_send.clear()
    M.Cfg.sends = 0
    M.Cfg.register_tries = 0
    M.Cfg.rate_limit_after = 0
    M.Cfg.send_code_fail = {BAD: "该号码暂不支持注册"}
    M.Cfg.sms_dir = sms_dir
    M.Cfg.codes_path = os.path.join(tmp, "codes.jsonl")

    csv = os.path.join(tmp, "pool.csv")
    with open(csv, "w", encoding="utf-8") as f:
        f.write("phone\n" + "\n".join(PHONES) + "\n")
    out = os.path.join(tmp, "probe.jsonl")

    dport = free_port()
    daemon = f"http://127.0.0.1:{dport}"
    d = subprocess.Popen([PY, SMSD, "--host", "127.0.0.1", "--port", str(dport),
                          "--secret", SECRET, "--state", os.path.join(tmp, "inbox.jsonl")],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = threading.Event()
    th = None
    try:
        for _ in range(80):
            try:
                requests.get(daemon + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.15)
        else:
            check("daemon ready", False)
            return 1

        with M.Server() as srv:
            base = f"http://127.0.0.1:{srv.port}"
            th = threading.Thread(target=forwarder, args=(sms_dir, daemon, stop), daemon=True)
            th.start()

            print("[1] vmax_probe vets a pool with one SMS per number")
            r = subprocess.run([
                PY, "vmax_probe.py", "--input", csv, "--out", out,
                "--captcha", "queue", "--queue-dir", tok_dir, "--queue-wait", "20",
                "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}"
                         f"&timeout=15&since={{since}}",
                "--sms-timeout", "30", "--delay", "0", "--workers", "2",
                "--base", base, "--redirect", base + "/auth/callback",
            ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180,
                env=dict(os.environ, VMAX_NOPE="1"))
            print("\n".join(r.stdout.strip().splitlines()[-4:]))
            if r.returncode != 1:
                print(r.stderr[-600:])
            check("exit 1 (one number refused)", r.returncode == 1, r.returncode)

            recs = {x["phone"]: x for x in
                    (json.loads(l) for l in open(out, encoding="utf-8") if l.strip())}
            check("three records", len(recs) == 3, len(recs))
            check("good #1 ok", recs.get(GOOD[0], {}).get("status") == "ok", recs.get(GOOD[0]))
            check("good #2 ok", recs.get(GOOD[1], {}).get("status") == "ok", recs.get(GOOD[1]))
            check("bad one rejected", recs.get(BAD, {}).get("status") == "send_rejected",
                  recs.get(BAD))

            print("[2] no account was created and no code was stored")
            check("zero /register writes", M.Cfg.register_tries == 0, M.Cfg.register_tries)
            blob = open(out, encoding="utf-8").read()
            codes = [v for v in M.Cfg.codes.values() if v]
            check("issued two codes", len(codes) == 2, codes)
            check("no raw code in probe.jsonl", all(c not in blob for c in codes), codes)
            check("only masked codes kept",
                  all(("code_masked" in recs[p]) and ("code_len" in recs[p]) for p in GOOD),
                  [recs[p] for p in GOOD])

            print("[3] token accounting: one per attempt, refused numbers still spend one")
            used = [n for n in os.listdir(tok_dir) if n.endswith(".used")]
            check("three tokens consumed", len(used) == 3, used)
            check("two left", len([n for n in os.listdir(tok_dir)
                                   if n.startswith("tok_") and n.endswith(".json")]) == 2)

            print("[4] summary carries the buckets")
            summ = json.load(open(out + ".summary.json", encoding="utf-8"))
            check("summary ok=2", summ.get("ok") == 2, summ)
            check("summary fail=1", summ.get("fail") == 1, summ)
            check("by_status recorded", summ.get("by_status", {}).get("send_rejected") == 1,
                  summ.get("by_status"))
            check("says no account", "never called" in summ.get("note", ""), summ.get("note"))

            print("[5] a pool of virtual numbers is filtered before any SMS")
            csv2 = os.path.join(tmp, "virt.csv")
            with open(csv2, "w", encoding="utf-8") as f:
                f.write("phone\n17000000001\n17100000002\n")
            out2 = os.path.join(tmp, "virt.jsonl")
            r2 = subprocess.run([PY, "vmax_probe.py", "--input", csv2, "--out", out2,
                                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                                 "--sms", "none", "--delay", "0"],
                                cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
            recs2 = [json.loads(l) for l in open(out2, encoding="utf-8") if l.strip()]
            check("both virtual", all(x.get("status") == "virtual_range" for x in recs2),
                  [x.get("status") for x in recs2])
            check("exit 1", r2.returncode == 1, r2.returncode)
            check("no send happened", M.Cfg.sends == 2, M.Cfg.sends)
    finally:
        stop.set()
        if th:
            th.join(timeout=2)
        d.terminate()
        try:
            d.wait(timeout=5)
        except Exception:
            d.kill()

    print("ALL PROBE TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
