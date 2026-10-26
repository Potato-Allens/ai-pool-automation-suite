# -*- coding: utf-8 -*-
"""Already-registered numbers: detectable at send-code, terminal afterwards.

/register/send-code is documented as "向未注册手机号发送 register 用途验证码"
(openapi: send_register_form_code_register_send_code_post), so a taken number is
refused *before* an SMS and before any /register write.  That signal must:
  * surface in vmax_probe as already_registered (not a generic send_rejected)
  * be classified as phone_taken by the runner
  * be terminal: a resume skips it instead of burning a token every run
  * NOT count toward --max-fail (a pool of recycled numbers would abort the batch)
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
from vmax_register import classify_register_error, is_phone_taken  # noqa: E402

PY = sys.executable
SMSD = os.path.join(ROOT, "vmax_smsd.py")
SECRET = "<TEST_SECRET>-taken"

TAKEN = "13500135041"
GOOD = "13500135042"
GOOD2 = "13500135043"
REJECT = "13500135044"

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


def seed_tokens(d, n):
    for i in range(n):
        p = os.path.join(d, f"tok_{i:04d}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"param": M.Cfg.token}, f)
        os.utime(p, (time.time() + i, time.time() + i))


def counts(d):
    names = os.listdir(d)
    return (sum(1 for n in names if n.endswith(".used")),
            sum(1 for n in names if n.startswith("tok_") and n.endswith(".json")))


def main() -> int:
    print("[1] classification: conservative, and 未注册 must not trip it")
    cases = [
        ("该手机号已注册，请直接登录", "phone_taken"),
        ("该手机号已被注册", "phone_taken"),
        ("账号已存在", "phone_taken"),
        ("手机号未注册", "other"),
        ("该手机号未被注册", "other"),
        ("验证码不正确", "code_wrong"),
        ("", "unknown"),
    ]
    for msg, want in cases:
        got = classify_register_error(msg)
        check(f"{msg[:16]!r} -> {want}", got == want, got)
    check("is_phone_taken('未注册') False", is_phone_taken("手机号未注册") is False)
    check("is_phone_taken('已注册') True", is_phone_taken("该号码已注册") is True)

    tmp = tempfile.mkdtemp(prefix="taken_")
    sms_dir = os.path.join(tmp, "sms")
    tok_dir = os.path.join(tmp, "tokens")
    os.makedirs(sms_dir, exist_ok=True)
    os.makedirs(tok_dir, exist_ok=True)

    M.Cfg.cooldown = 0
    M.Cfg.codes.clear()
    M.Cfg.attempts.clear()
    M.Cfg.last_send.clear()
    M.Cfg.sends = 0
    M.Cfg.register_tries = 0
    M.Cfg.rate_limit_after = 0
    M.Cfg.omit_code = False
    M.Cfg.reject_first = 0
    M.Cfg.phone_taken = {TAKEN}
    M.Cfg.send_code_fail = {REJECT: "该号码暂不支持注册"}
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
            sms = (f"http:{daemon}/code?phone={{phone}}&secret={SECRET}"
                   f"&timeout=15&since={{since}}")

            print("[2] probe: taken number is named, not lumped into send_rejected")
            seed_tokens(tok_dir, 3)
            csv = os.path.join(tmp, "pool.csv")
            with open(csv, "w", encoding="utf-8") as f:
                f.write("phone\n" + "\n".join([TAKEN, GOOD, REJECT]) + "\n")
            out = os.path.join(tmp, "probe.jsonl")
            r = subprocess.run([PY, "vmax_probe.py", "--input", csv, "--out", out,
                                "--captcha", "queue", "--queue-dir", tok_dir,
                                "--queue-wait", "20", "--sms", sms, "--sms-timeout", "30",
                                "--delay", "0", "--workers", "1",
                                "--base", base, "--redirect", base + "/auth/callback"],
                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180)
            check("exit 1 (not every number is usable)", r.returncode == 1, r.returncode)
            recs = {x["phone"]: x for x in
                    (json.loads(l) for l in open(out, encoding="utf-8") if l.strip())}
            check("taken -> already_registered",
                  recs.get(TAKEN, {}).get("status") == "already_registered", recs.get(TAKEN))
            check("good -> ok", recs.get(GOOD, {}).get("status") == "ok", recs.get(GOOD))
            check("refused -> send_rejected",
                  recs.get(REJECT, {}).get("status") == "send_rejected", recs.get(REJECT))
            check("taken carries the server message",
                  "已注册" in str(recs.get(TAKEN, {}).get("error")), recs.get(TAKEN))
            check("no SMS went to the taken number", M.Cfg.sends == 1, M.Cfg.sends)
            check("zero /register writes", M.Cfg.register_tries == 0, M.Cfg.register_tries)
            summ = json.load(open(out + ".summary.json", encoding="utf-8"))
            check("by_status has already_registered",
                  summ.get("by_status", {}).get("already_registered") == 1, summ.get("by_status"))
            used, left = counts(tok_dir)
            check("three tokens spent (one attempt each)", used == 3, used)
            check("none left", left == 0, left)

            print("[3] batch: phone_taken is terminal and costs no SMS")
            seed_tokens(tok_dir, 2)
            M.Cfg.sends = 0
            M.Cfg.register_tries = 0
            M.Cfg.codes.clear()
            csv2 = os.path.join(tmp, "batch.csv")
            with open(csv2, "w", encoding="utf-8") as f:
                f.write("phone\n" + TAKEN + "\n" + GOOD + "\n")
            out2 = os.path.join(tmp, "results.jsonl")
            r = subprocess.run([PY, "vmax_batch.py", "--input", csv2, "--out", out2,
                                "--workers", "1", "--delay", "0", "--captcha", "queue",
                                "--queue-dir", tok_dir, "--queue-wait", "20",
                                "--sms", sms, "--sms-resends", "0",
                                "--base", base, "--redirect", base + "/auth/callback"],
                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180,
                               env=dict(os.environ, VMAX_RESEND_WAIT="0",
                                        VMAX_PACE_STATE=os.path.join(tmp, "pace.json")))
            rows = [json.loads(l) for l in open(out2, encoding="utf-8") if l.strip()]
            by = {x["phone"]: x for x in rows}
            check("two rows", len(rows) == 2, rows)
            check("taken row is an error", by.get(TAKEN, {}).get("status") == "error",
                  by.get(TAKEN))
            check("taken row is classified",
                  by.get(TAKEN, {}).get("register_error_kind") == "phone_taken", by.get(TAKEN))
            # R104: the failure must carry its partial row, not just a string.  Before
            # the fix this row had no send_code at all (the answer was thrown away
            # with the bare VmaxError), which is how a real "already registered"
            # number stayed indistinguishable from a generic error.
            check("taken row keeps the send-code answer",
                  (by.get(TAKEN, {}).get("send_code") or {}).get("ok") is False, by.get(TAKEN))
            check("good row registered", by.get(GOOD, {}).get("status") == "ok", by.get(GOOD))
            check("only one /register write", M.Cfg.register_tries == 1, M.Cfg.register_tries)
            check("only one SMS sent", M.Cfg.sends == 1, M.Cfg.sends)
            check("exit 7 (nothing failed, one number was taken)", r.returncode == 7,
                  (r.returncode, r.stdout[-300:]))
            check("summary line names taken", "taken=1" in r.stdout, r.stdout[-300:])

            print("[4] resume: taken phone is skipped, not retried")
            used0, left0 = counts(tok_dir)
            n_rows = len(rows)
            r = subprocess.run([PY, "vmax_batch.py", "--input", csv2, "--out", out2,
                                "--workers", "1", "--delay", "0", "--captcha", "queue",
                                "--queue-dir", tok_dir, "--queue-wait", "5",
                                "--sms", sms, "--sms-resends", "0",
                                "--base", base, "--redirect", base + "/auth/callback"],
                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            check("resume exit 0 (nothing left to do)", r.returncode == 0, r.returncode)
            check("says 0 to run", "0 to run" in r.stdout, r.stdout[-400:])
            check("reports the skipped number",
                  "already registered elsewhere -> skipped" in r.stdout, r.stdout[-400:])
            check("no new token spent", counts(tok_dir) == (used0, left0), counts(tok_dir))
            check("no new row written",
                  len([l for l in open(out2, encoding="utf-8") if l.strip()]) == n_rows)
            check("no extra SMS", M.Cfg.sends == 1, M.Cfg.sends)

            print("[5] --max-fail is not eaten by already-registered numbers")
            seed_tokens(tok_dir, 3)
            M.Cfg.sends = 0
            M.Cfg.register_tries = 0
            M.Cfg.codes.clear()
            csv3 = os.path.join(tmp, "three.csv")
            with open(csv3, "w", encoding="utf-8") as f:
                f.write("phone\n" + TAKEN + "\n" + GOOD + "\n" + GOOD2 + "\n")
            out3 = os.path.join(tmp, "results3.jsonl")
            r = subprocess.run([PY, "vmax_batch.py", "--input", csv3, "--out", out3,
                                "--workers", "1", "--delay", "0", "--max-fail", "1",
                                "--captcha", "queue", "--queue-dir", tok_dir,
                                "--queue-wait", "20", "--sms", sms, "--sms-resends", "0",
                                "--base", base, "--redirect", base + "/auth/callback"],
                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=240)
            rows3 = {json.loads(l)["phone"]: json.loads(l)
                     for l in open(out3, encoding="utf-8") if l.strip()}
            check("all three rows attempted", len(rows3) == 3, list(rows3))
            check("both good rows ok",
                  all(rows3.get(p, {}).get("status") == "ok" for p in (GOOD, GOOD2)), rows3)
            check("run was not stopped by --max-fail",
                  "reached" not in r.stdout, r.stdout[-400:])
            check("exit 7 again", r.returncode == 7, r.returncode)
    finally:
        stop.set()
        if th:
            th.join(timeout=2)
        d.terminate()
        try:
            d.wait(timeout=5)
        except Exception:
            d.kill()

    print()
    if fails:
        print(f"FAILED {len(fails)}: " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
