# -*- coding: utf-8 -*-
"""Parallel batch correctness: 4 accounts at once, SMS arriving out of order.

Only --workers > 1 can break these, so each one gets its own assertion:

  1. every worker waits on *its own* phone  -> the mock records ZERO wrong-code
     attempts (a cross-talk bug would submit a neighbour's code and show up here)
  2. the captcha token queue is popped atomically -> 4 pops, 4 tokens left,
     no token used twice
  3. the cross-process pacer does not lose updates -> window_used == 4 after
     the run (a read-modify-write race would drop entries)
  4. the runner exits cleanly with the documented code
     (regression: `stopped` was only initialised on the sequential branch, so
     --workers 4 ended with UnboundLocalError *after* writing every result)
  5. with --on-taken reset the 2-strike breaker survives concurrency: four
     lanes all hit phone_taken at once, and the run still spends two tokens
     (a lane re-reads the switch after winning the reset gate)
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
SECRET = "<TEST_SECRET>-par"

PHONES = ["13500135001", "13500135002", "13500135003", "13500135004"]
# Deliberately out of order: the slowest phone is first in the file.
DELAY = {PHONES[0]: 5.0, PHONES[1]: 1.5, PHONES[2]: 0.0, PHONES[3]: 3.5}

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


def forwarder(sms_dir: str, daemon: str, stop: threading.Event):
    """Stand-in for the SIM farm: each phone's SMS lands after its own delay."""
    seen: dict[str, float] = {}
    pending: list[dict] = []
    while not stop.is_set():
        now = time.time()
        try:
            for name in os.listdir(sms_dir):
                if not name.endswith(".txt"):
                    continue
                path = os.path.join(sms_dir, name)
                mtime = os.path.getmtime(path)
                if seen.get(path) == mtime:
                    continue
                seen[path] = mtime
                phone = name[:-4]
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                pending.append({"due": mtime + DELAY.get(phone, 0.0),
                                "phone": phone, "text": text})
        except OSError:
            pass
        keep = []
        for item in pending:
            if now >= item["due"]:
                try:
                    requests.post(daemon + "/ingest",
                                  json={"phone": item["phone"], "text": item["text"]},
                                  headers={"X-Sms-Secret": SECRET}, timeout=5)
                except Exception:  # noqa: BLE001 - keep the harness alive
                    keep.append(item)
            else:
                keep.append(item)
        pending = keep
        stop.wait(0.1)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="batch_par_")
    sms_dir = os.path.join(tmp, "sms")
    tok_dir = os.path.join(tmp, "tokens")
    os.makedirs(sms_dir, exist_ok=True)
    os.makedirs(tok_dir, exist_ok=True)

    # 8 tokens for 4 accounts: enough headroom to see double-pops, and the
    # leftovers prove the queue was not drained by mistake.
    for i in range(8):
        path = os.path.join(tok_dir, f"tok_{i:04d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"param": M.Cfg.token, "ts": time.time()}, f)
        os.utime(path, (time.time() + i, time.time() + i))

    M.Cfg.cooldown = 0
    M.Cfg.reject_first = 0
    M.Cfg.omit_code = False
    M.Cfg.codes.clear()
    M.Cfg.attempts.clear()
    M.Cfg.last_send.clear()
    M.Cfg.sends = 0
    M.Cfg.register_tries = 0
    M.Cfg.sms_dir = sms_dir
    M.Cfg.codes_path = os.path.join(tmp, "codes.jsonl")

    csv = os.path.join(tmp, "accounts.csv")
    with open(csv, "w", encoding="utf-8") as f:
        f.write("phone\n" + "\n".join(PHONES) + "\n")
    results = os.path.join(tmp, "results.jsonl")
    pace = os.path.join(tmp, "pace.json")

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

            print("[1] vmax_batch --workers 4 runs four accounts at once")
            t0 = time.time()
            r = subprocess.run([
                PY, "vmax_batch.py",
                "--input", csv, "--out", results,
                "--workers", "4", "--delay", "0",
                "--captcha", "queue", "--queue-dir", tok_dir,
                "--captcha-attempts", "1", "--form-captcha", "auto",
                "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}"
                         f"&timeout=30&since={{since}}",
                "--sms-resends", "0",
                "--base", base, "--redirect", base + "/auth/callback",
            ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=240,
                env=dict(os.environ, VMAX_PACE_STATE=pace, VMAX_RESEND_WAIT="0",
                         VMAX_TOKEN_MAX_AGE="600"))
            wall = time.time() - t0
            tail = "\n".join(r.stdout.strip().splitlines()[-4:])
            print(tail)
            if r.returncode != 0:
                print(r.stderr[-800:])
                print(r.stdout[-1500:])
            check("exit 0 (regression: UnboundLocalError on `stopped`)",
                  r.returncode == 0, r.returncode)
            check("no traceback", "Traceback" not in (r.stdout + r.stderr))
            check("slowest phone really waited", wall >= 4.5, f"{wall:.1f}s")

            print("[2] results.jsonl: four distinct successful accounts")
            recs = [json.loads(l) for l in open(results, encoding="utf-8") if l.strip()]
            check("four records", len(recs) == 4, len(recs))
            check("unique phones", {x.get("phone") for x in recs} == set(PHONES),
                  sorted(x.get("phone") for x in recs))
            for x in recs:
                print("   ", x.get("phone"), x.get("status"),
                      str(x.get("error"))[:160])
            check("all ok", all(x.get("status") == "ok" for x in recs),
                  [x.get("status") for x in recs])
            check("all have uid", all(x.get("uid") for x in recs))
            regs = [x.get("register") or {} for x in recs]
            check("all 303", all(g.get("status") == 303 for g in regs),
                  [g.get("status") for g in regs])
            check("all carry code+state",
                  all("code=" in (g.get("location") or "") and
                      "state=" in (g.get("location") or "") for g in regs))

            print("[3] no cross-phone code: every submit hit on the first try")
            check("one register write per account", M.Cfg.register_tries == 4,
                  M.Cfg.register_tries)
            wrong = {p: n for p, n in M.Cfg.attempts.items() if n}
            check("zero wrong-code attempts", not wrong, wrong)
            check("four send-codes, no resend", M.Cfg.sends == 4, M.Cfg.sends)

            print("[4] token queue popped atomically")
            used = [n for n in os.listdir(tok_dir) if n.endswith(".used")]
            left = [n for n in os.listdir(tok_dir) if n.startswith("tok_") and n.endswith(".json")]
            check("four tokens consumed", len(used) == 4, used)
            check("four tokens left", len(left) == 4, left)

            print("[5] pacer kept every write in the window (no lost update)")
            with open(pace, encoding="utf-8") as f:
                pdata = json.load(f)
            key = next(iter(pdata.get("keys") or {}), "register@direct")
            entry = pdata["keys"][key]
            check("window_used == 4", len(entry.get("times") or []) == 4,
                  len(entry.get("times") or []))
            check("no ban recorded", not entry.get("ban_until"), entry.get("ban_until"))
            check("pacer key names the lane", key.startswith("register@"), key)

            print("[6] smsd stored one code per phone")
            r = requests.get(daemon + "/latest?limit=20&secret=" + SECRET, timeout=10)
            items = (r.json() or {}).get("items") or []
            check("four inbox items", len(items) == 4, len(items))
            got = {i["phone"]: i.get("code") for i in items}
            want = {p: M.Cfg.codes.get(p) for p in PHONES}
            check("phone -> code mapping intact", got == want, {"got": got, "want": want})
            print("[7] workers=4 + --on-taken reset: the 2-strike breaker holds")
            # All four lanes can answer phone_taken in the same instant.  Each one
            # used to read reset_state["off"] *outside* the lock, so four resets
            # started before any of them recorded a 429 -- the R106 cap was
            # multiplied by --workers.  Reset now runs behind its own gate and the
            # switch is re-read after winning it, so the burn stays at two tokens
            # however many lanes are running.
            tok7 = os.path.join(tmp, "tokens7")
            os.makedirs(tok7, exist_ok=True)
            for i in range(8):
                p7 = os.path.join(tok7, "tok_%04d.json" % i)
                with open(p7, "w", encoding="utf-8") as f:
                    json.dump({"param": M.Cfg.token, "ts": time.time()}, f)
                os.utime(p7, (time.time() + i, time.time() + i))
            M.Cfg.phone_taken = set(PHONES)     # every number is already registered
            M.Cfg.reset_rate_limited = True     # ... and today's reset quota is gone
            # A real reset spends 10-120 s on the captcha *before* the 429 comes
            # back, so every lane is inside reset_one long before the first strike
            # is recorded.  Without this the mock answers in ~1 ms, the window is
            # microseconds wide and the race only shows up on some runs.
            M.Cfg.reset_delay = 0.4
            sends7 = M.Cfg.sends
            out7 = os.path.join(tmp, "results7.jsonl")
            r7 = subprocess.run([
                PY, "vmax_batch.py",
                "--input", csv, "--out", out7,
                "--workers", "4", "--delay", "0",
                "--captcha", "queue", "--queue-dir", tok7, "--queue-wait", "10",
                "--captcha-attempts", "1", "--form-captcha", "auto",
                "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}"
                         f"&timeout=20&since={{since}}",
                "--sms-resends", "0",
                "--on-taken", "reset",
                "--base", base, "--redirect", base + "/auth/callback",
            ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300,
                env=dict(os.environ, VMAX_PACE_STATE=os.path.join(tmp, "pace7.json"),
                         VMAX_RESEND_WAIT="0", VMAX_TOKEN_MAX_AGE="600"))
            M.Cfg.reset_delay = 0.0
            rows7 = [json.loads(l) for l in open(out7, encoding="utf-8") if l.strip()]
            resets7 = [x for x in rows7 if x.get("via") == "reset"]
            used7 = [n for n in os.listdir(tok7) if n.endswith(".used")]
            print("    exit", r7.returncode, "| reset rows", len(resets7),
                  "| tokens used", len(used7), "| rows", len(rows7))
            if r7.returncode != 7:
                print(r7.stdout[-1500:])
                print(r7.stderr[-800:])
            check("exit 7 (pool is all taken)", r7.returncode == 7, r7.returncode)
            check("no traceback", "Traceback" not in (r7.stdout + r7.stderr))
            check("at most 2 reset attempts under 4 lanes", len(resets7) <= 2,
                  len(resets7))
            check("both strikes recorded", "limited=2" in r7.stdout,
                  r7.stdout[-500:])
            # 4 register attempts + one token per reset attempt: nothing else spent.
            check("reset spent exactly one token per attempt",
                  len(used7) == 4 + len(resets7),
                  {"tokens": len(used7), "resets": len(resets7)})
            check("breaker announced itself",
                  "no more resets this run" in r7.stdout, r7.stdout[-700:])
            check("zero SMS", M.Cfg.sends == sends7, M.Cfg.sends - sends7)
            check("one taken row per number",
                  len([x for x in rows7
                       if x.get("register_error_kind") == "phone_taken"]) == 4,
                  [x.get("register_error_kind") for x in rows7])

            print("[7b] same 4 lanes, healthy reset channel: the gate still recovers all")
            # The gate serialises resets; it must not turn into a deadlock or a
            # "one reset and stop" loop.  Each number needs its own SMS, so this
            # also proves the per-phone wait survives being queued behind a peer.
            tok7b = os.path.join(tmp, "tokens7b")
            os.makedirs(tok7b, exist_ok=True)
            for i in range(12):
                p7b = os.path.join(tok7b, "tok_%04d.json" % i)
                with open(p7b, "w", encoding="utf-8") as f:
                    json.dump({"param": M.Cfg.token, "ts": time.time()}, f)
                os.utime(p7b, (time.time() + i, time.time() + i))
            M.Cfg.reset_rate_limited = False
            M.Cfg.codes.clear()
            M.Cfg.sends = 0
            out7b = os.path.join(tmp, "results7b.jsonl")
            r7b = subprocess.run([
                PY, "vmax_batch.py",
                "--input", csv, "--out", out7b,
                "--workers", "4", "--delay", "0",
                "--captcha", "queue", "--queue-dir", tok7b, "--queue-wait", "10",
                "--captcha-attempts", "1", "--form-captcha", "auto",
                "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}"
                         f"&timeout=30&since={{since}}",
                "--sms-resends", "0",
                "--on-taken", "reset",
                "--base", base, "--redirect", base + "/auth/callback",
            ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=420,
                env=dict(os.environ, VMAX_PACE_STATE=os.path.join(tmp, "pace7b.json"),
                         VMAX_RESEND_WAIT="0", VMAX_TOKEN_MAX_AGE="600"))
            rows7b = [json.loads(l) for l in open(out7b, encoding="utf-8") if l.strip()]
            resets7b = [x for x in rows7b if x.get("via") == "reset"]
            used7b = [n for n in os.listdir(tok7b) if n.endswith(".used")]
            print("    exit", r7b.returncode, "| reset ok",
                  len([x for x in resets7b if x.get("status") == "ok"]),
                  "| tokens", len(used7b))
            if r7b.returncode != 0:
                print(r7b.stdout[-2000:])
                print(r7b.stderr[-800:])
            check("exit 0 (every number recovered)", r7b.returncode == 0,
                  r7b.returncode)
            check("no traceback", "Traceback" not in (r7b.stdout + r7b.stderr))
            check("all four reset rows ok",
                  [x.get("phone") for x in resets7b if x.get("status") == "ok"] != []
                  and len([x for x in resets7b if x.get("status") == "ok"]) == 4,
                  [(x.get("phone"), x.get("status")) for x in resets7b])
            check("unique recovered phones",
                  {x.get("phone") for x in resets7b} == set(PHONES),
                  sorted(str(x.get("phone")) for x in resets7b))
            check("summary says reused=4 taken=0",
                  "reused=4" in r7b.stdout and "taken=0" in r7b.stdout,
                  r7b.stdout[-400:])
            check("no cap recorded", "limited=0" in r7b.stdout, r7b.stdout[-400:])
            check("one SMS per recovered number", M.Cfg.sends == 4, M.Cfg.sends)
            check("8 tokens: 4 registers + 4 resets", len(used7b) == 8, len(used7b))
            check("each recovery carries a uid",
                  all(x.get("uid") for x in resets7b),
                  [x.get("uid") for x in resets7b])

    finally:
        stop.set()
        if th:
            th.join(timeout=2)
        d.terminate()
        try:
            d.wait(timeout=5)
        except Exception:  # noqa: BLE001
            d.kill()

    print("ALL PARALLEL BATCH TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
