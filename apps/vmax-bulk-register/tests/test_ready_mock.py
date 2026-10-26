# -*- coding: utf-8 -*-
"""Day-one readiness (`vmax_ready.py`) against the local mock: no network, no SMS.

    [1] one card, real CLIs       -> exit 0, six criteria, READY.md, cost 1/1/1
    [2] dead SMS channel          -> exit 9, 0 token, 0 SMS, no result row
    [3] a taken card              -> exit 1, the failing criterion is named
    [4] --dry-run                 -> exit 0 and it does not even create the dir
    [5] --contract                -> handed to vmax_day.py (the gate lives there)
    [6] the same --out again      -> exit 2 dirty_out, 0 SMS (R139: this used to be READY)
    [7] same --out + --resume     -> exit 1 stale_artifacts, 0 SMS, 0 new rows
    [8] a journal-recovered row   -> exit 1, row_fresh + sms_this_run both FAIL
    [9] --form-captcha device     -> ready->day->batch->register on 1 token / 1 SMS

The point of the suite is not the mock: it is that `READY` is computed from the
artifacts (results.jsonl / logins.jsonl), not from a child exiting 0.  [3] is the
proof -- the wrapper runs fine but the verdict is NOT_READY.  [6]/[7] are the
provenance proof -- before R139 a *second* run into the same --out printed READY
having spent 0 SMS and 0 tokens, because vmax_day exits 4 and the old row was
still there; [8] is the same trap in the other shape, a row rebuilt from the
write-ahead journal (0 SMS) that must never count as a card.
"""
from __future__ import annotations

import csv
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

import mock_e2e as M  # noqa: E402
import vmax_journal as J  # noqa: E402

PY = sys.executable
SMSD = os.path.join(ROOT, "vmax_smsd.py")
SECRET = "<TEST_SECRET>-ready"
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


def start_sync(sms_dir):
    """The mock keeps codes in memory; the file provider reads <phone>.txt."""
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


def write_cards(path, phones):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone", "username", "password", "proxy"])
        for p in phones:
            w.writerow([p, "", "", ""])


def run_ready(td, cards, out_name, sms_dir, extra=(), timeout=180):
    out = os.path.join(td, out_name)
    env = dict(os.environ, VMAX_NOPACE="1", VMAX_RESEND_WAIT="0", PYTHONIOENCODING="utf-8",
               VMAX_PACE_STATE=os.path.join(td, "pace_" + out_name + ".json"))
    cmd = [PY, "-u", os.path.join(ROOT, "vmax_ready.py"),
           "--input", cards, "--out", out,
           "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "15",
           "--sms-resends", "0",
           "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback",
           "--captcha", "param", "--captcha-param", M.Cfg.token,
           "--skip-preflight", "--yes"] + list(extra)
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env, timeout=timeout)
    rep = {}
    rp = os.path.join(out, "ready.json")
    if os.path.exists(rp):
        try:
            rep = json.load(open(rp, encoding="utf-8"))
        except ValueError:
            rep = {}
    return cp, out, rep


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8-sig") if l.strip()]

def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ready_mock_")
    sms_dir = os.path.join(tmp, "sms")
    os.makedirs(sms_dir, exist_ok=True)
    M.Cfg.codes_path = os.path.join(tmp, "codes.jsonl")
    M.Cfg.sms_dir = sms_dir

    dport = free_port()
    daemon = "http://127.0.0.1:%d" % dport
    d = subprocess.Popen([PY, SMSD, "--host", "127.0.0.1", "--port", str(dport),
                          "--secret", SECRET, "--state", os.path.join(tmp, "inbox.jsonl")],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop, th = None, None
    try:
        import requests
        for _ in range(80):
            try:
                requests.get(daemon + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        with M.Server() as srv:
            stop, th = start_sync(sms_dir)

            print("[1] one card through the real CLIs -> READY")
            reset_mock()
            phone = "13700137101"
            cards = os.path.join(tmp, "cards.csv")
            write_cards(cards, [phone, "13700137102"])
            cp, out, rep = run_ready(tmp, cards, "run1", sms_dir,
                                     extra=("--smsd", daemon, "--secret", SECRET))
            if cp.returncode != 0:
                print("--- stdout tail ---")
                print(cp.stdout[-2500:])
                print("--- stderr tail ---")
                print(cp.stderr[-1500:])
            check("1 exit 0", cp.returncode == 0, cp.returncode)
            check("1 verdict ready", rep.get("verdict") == "ready", rep.get("verdict"))
            names = {c["name"]: c["ok"] for c in (rep.get("checks") or [])}
            check("1 six hard criteria, all true",
                  len(names) == 6 and all(names.values()), names)
            check("1 the criteria are the expected six",
                  set(names) == {"register_303", "location_code", "location_state",
                                 "access_token", "uid", "login_ok"}, sorted(names))
            prov = {c["name"]: c["ok"] for c in (rep.get("provenance") or [])}
            check("1 provenance: row_fresh + sms_this_run, both true",
                  set(prov) == {"row_fresh", "sms_this_run"} and all(prov.values()), prov)
            cost = rep.get("cost") or {}
            check("1 cost: 1 token / 1 SMS / 1 write",
                  (cost.get("captcha_tokens"), cost.get("sms"), cost.get("register_writes"))
                  == (1, 1, 1), cost)
            # R150 named the reuse path; R151 stops the judge from calling it a
            # non-reuse (the old tuple only knew None/auto/device).
            soft1 = {c["name"]: c["ok"] for c in (rep.get("soft") or [])}
            check("1 captcha_reused accepts reuse", soft1.get("captcha_reused") is True, soft1)
            check("1 cost: no extra form token",
                  cost.get("form_extra_tokens") == 0, cost)
            md = os.path.join(out, "READY.md")
            text = open(md, encoding="utf-8").read() if os.path.exists(md) else ""
            check("1 READY.md carries the verdict", "判定：READY" in text, text[:200])
            check("1 READY.md has all six rows", text.count("| `") >= 6, text.count("| `"))
            rows = read_jsonl(os.path.join(out, "results.jsonl"))
            logins = read_jsonl(os.path.join(out, "logins.jsonl"))
            check("1 one result row, ok", len(rows) == 1 and rows[0].get("status") == "ok",
                  [(r.get("phone"), r.get("status")) for r in rows])
            check("1 the row says reuse (auto reused the send-code token)",
                  rows[0].get("form_captcha") == "reuse", rows[0].get("form_captcha"))
            check("1 one login row, ok",
                  len(logins) == 1 and logins[0].get("status") == "ok", logins)
            check("1 one register write", M.Cfg.register_tries == 1, M.Cfg.register_tries)
            check("1 exactly one SMS", M.Cfg.sends == 1, M.Cfg.sends)
            one = open(os.path.join(out, "card1.csv"), encoding="utf-8").read().strip().splitlines()
            check("1 card1.csv holds exactly one card", len(one) == 2 and phone in one[1], one)
            acc = os.path.join(out, "accept.json")
            check("1 the accept step left its own artifact",
                  os.path.exists(acc) and json.load(open(acc, encoding="utf-8"))["ready"] is True,
                  acc)

            print("[2] a dead SMS channel costs nothing")
            reset_mock()
            tries2, sends2 = M.Cfg.register_tries, M.Cfg.sends
            cp2, out2, rep2 = run_ready(
                tmp, cards, "run2", sms_dir,
                extra=("--no-accept", "--sms", "http:http://127.0.0.1:1/code?phone={phone}"))
            check("2 exit 9", cp2.returncode == 9, (cp2.returncode, cp2.stderr[-300:]))
            check("2 verdict sms_channel_down",
                  rep2.get("verdict") == "sms_channel_down", rep2.get("verdict"))
            check("2 no results file", not os.path.exists(os.path.join(out2, "results.jsonl")))
            check("2 no token burned", M.Cfg.register_tries == tries2, M.Cfg.register_tries - tries2)
            check("2 no SMS sent", M.Cfg.sends == sends2, M.Cfg.sends - sends2)

            print("[3] a taken card: the wrapper runs, the verdict is NOT_READY")
            reset_mock()
            taken = "13700137103"
            M.Cfg.phone_taken = {taken}
            cards3 = os.path.join(tmp, "cards3.csv")
            write_cards(cards3, [taken])
            cp3, out3, rep3 = run_ready(tmp, cards3, "run3", sms_dir,
                                        extra=("--no-accept",))
            check("3 exit 1", cp3.returncode == 1, (cp3.returncode, cp3.stderr[-300:]))
            check("3 verdict not_ready", rep3.get("verdict") == "not_ready", rep3.get("verdict"))
            failed = [c["name"] for c in (rep3.get("checks") or []) if not c["ok"]]
            check("3 register_303 is the named failure", "register_303" in failed, failed)
            check("3 the headline names it", "register_303" in (rep3.get("headline") or ""),
                  rep3.get("headline"))
            check("3 the taken row spent no SMS", M.Cfg.sends == 0, M.Cfg.sends)
            md3 = os.path.join(out3, "READY.md")
            text3 = open(md3, encoding="utf-8").read() if os.path.exists(md3) else ""
            check("3 READY.md shows a FAIL cell", "**FAIL**" in text3, text3[:300])

            print("[4] --dry-run prints the plan and creates nothing")
            out4 = os.path.join(tmp, "run4")
            env4 = dict(os.environ, VMAX_NOPACE="1", PYTHONIOENCODING="utf-8")
            cp4 = subprocess.run(
                [PY, "-u", os.path.join(ROOT, "vmax_ready.py"), "--input", cards,
                 "--out", out4, "--smsd", daemon, "--secret", SECRET, "--dry-run"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env4, timeout=60)
            check("4 exit 0", cp4.returncode == 0, cp4.returncode)
            check("4 prints the three steps",
                  "probe" in cp4.stdout and "day" in cp4.stdout
                  and "accept" in cp4.stdout, cp4.stdout[-400:])
            check("4 the secret is masked", SECRET not in cp4.stdout, cp4.stdout[-400:])
            check("4 nothing was created", not os.path.exists(out4), out4)

            print("[5] --contract is handed to vmax_day.py (the gate lives there)")
            out5 = os.path.join(tmp, "run5")
            cp5 = subprocess.run(
                [PY, "-u", os.path.join(ROOT, "vmax_ready.py"), "--input", cards,
                 "--out", out5, "--smsd", daemon, "--secret", SECRET,
                 "--dry-run", "--contract", os.path.join(tmp, "contract.json")],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env4, timeout=60)
            check("5 exit 0", cp5.returncode == 0, cp5.returncode)
            check("5 the day command carries --contract and the absolute baseline",
                  "--contract " in cp5.stdout
                  and os.path.join(tmp, "contract.json") in cp5.stdout,
                  cp5.stdout[-600:])
            check("5 still nothing was created", not os.path.exists(out5), out5)
            check("5 the secret stays masked", SECRET not in cp5.stdout, cp5.stdout[-400:])

            print("[6] the same --out again must not become READY (it spent nothing)")
            before_rows = read_jsonl(os.path.join(out, "results.jsonl"))
            reset_mock()
            cp6, out6, rep6 = run_ready(tmp, cards, "run1", sms_dir, extra=("--no-accept",))
            check("6 exit 2", cp6.returncode == 2, (cp6.returncode, cp6.stderr[-300:]))
            check("6 verdict dirty_out", rep6.get("verdict") == "dirty_out", rep6.get("verdict"))
            check("6 it says how many rows are already there",
                  (rep6.get("run") or {}).get("results_before") == len(before_rows),
                  rep6.get("run"))
            check("6 0 SMS and 0 register writes", (M.Cfg.sends, M.Cfg.register_tries) == (0, 0),
                  (M.Cfg.sends, M.Cfg.register_tries))
            after_rows = read_jsonl(os.path.join(out, "results.jsonl"))
            check("6 the old rows are untouched", after_rows == before_rows,
                  (len(before_rows), len(after_rows)))
            md6 = os.path.join(out, "READY.md")
            text6 = open(md6, encoding="utf-8").read() if os.path.exists(md6) else ""
            check("6 the stale READY.md is replaced by DIRTY_OUT", "判定：DIRTY_OUT" in text6,
                  text6[:200])

            print("[7] --resume on that dir: only this run's rows count")
            cp7, out7, rep7 = run_ready(tmp, cards, "run1", sms_dir,
                                        extra=("--no-accept", "--resume"))
            check("7 exit 1", cp7.returncode == 1, (cp7.returncode, cp7.stderr[-300:]))
            check("7 verdict stale_artifacts", rep7.get("verdict") == "stale_artifacts",
                  rep7.get("verdict"))
            run7 = rep7.get("run") or {}
            check("7 0 new rows, total unchanged",
                  run7.get("results_new") == 0 and run7.get("results_total") == len(before_rows),
                  run7)
            check("7 the headline says the rows are last run's",
                  "上一轮" in (rep7.get("headline") or ""), rep7.get("headline"))
            check("7 still 0 SMS and 0 writes", (M.Cfg.sends, M.Cfg.register_tries) == (0, 0),
                  (M.Cfg.sends, M.Cfg.register_tries))

            print("[8] a journal-recovered row is not a card (0 SMS, no token)")
            reset_mock()
            out8 = os.path.join(tmp, "run8")
            os.makedirs(out8, exist_ok=True)
            jpath = os.path.join(out8, "results.jsonl" + J.SUFFIX)
            J.append(jpath, phone, "ok", username="u_%s" % phone, password="pw-journal",
                     uid="UIDJOURNAL1")
            cp8, out8, rep8 = run_ready(tmp, cards, "run8", sms_dir, extra=("--no-accept",))
            check("8 exit 1", cp8.returncode == 1, (cp8.returncode, cp8.stderr[-300:]))
            check("8 verdict not_ready", rep8.get("verdict") == "not_ready", rep8.get("verdict"))
            prov8 = {c["name"]: c["ok"] for c in (rep8.get("provenance") or [])}
            check("8 row_fresh FAIL", prov8.get("row_fresh") is False, prov8)
            check("8 sms_this_run FAIL", prov8.get("sms_this_run") is False, prov8)
            check("8 both are named in the headline",
                  "row_fresh" in (rep8.get("headline") or "")
                  and "sms_this_run" in (rep8.get("headline") or ""), rep8.get("headline"))
            check("8 the recovered row really landed (1 new row)",
                  (rep8.get("run") or {}).get("results_new") == 1, rep8.get("run"))
            check("8 it cost 0 SMS and 0 register writes",
                  (M.Cfg.sends, M.Cfg.register_tries) == (0, 0),
                  (M.Cfg.sends, M.Cfg.register_tries))
            md8 = os.path.join(out8, "READY.md")
            text8 = open(md8, encoding="utf-8").read() if os.path.exists(md8) else ""
            check("8 READY.md shows the provenance FAIL", "出处判据" in text8
                  and "**FAIL**" in text8, text8[:400])
            print("[9] --form-captcha device survives ready -> day -> batch -> register")
            reset_mock()
            phone9 = "13700137109"
            cards9 = os.path.join(tmp, "cards9.csv")
            write_cards(cards9, [phone9])
            cp9, out9, rep9 = run_ready(tmp, cards9, "run9", sms_dir,
                                        extra=("--smsd", daemon, "--secret", SECRET,
                                               "--form-captcha", "device"))
            check("9 exit 0", cp9.returncode == 0,
                  (cp9.returncode, (cp9.stdout or "")[-600:]))
            check("9 verdict ready", rep9.get("verdict") == "ready", rep9.get("verdict"))
            cost9 = rep9.get("cost") or {}
            check("9 cost is 1 token / 1 SMS / 1 write",
                  (cost9.get("captcha_tokens"), cost9.get("sms"),
                   cost9.get("register_writes")) == (1, 1, 1), cost9)
            soft9 = {c["name"]: c["ok"] for c in (rep9.get("soft") or [])}
            check("9 captcha_reused accepts device", soft9.get("captcha_reused") is True, soft9)
            # ready.json carries only the verdict (checks / provenance / soft / cost) --
            # never the raw row.  The row is results.jsonl, so the device evidence has
            # to be read from there; asking ready.json for "row" returns {} and makes
            # a green run look red (this was the R145 bug in the first draft of [9]).
            rows9 = read_jsonl(os.path.join(out9, "results.jsonl"))
            row9 = rows9[0] if rows9 else {}
            check("9 results.jsonl holds exactly one row", len(rows9) == 1, len(rows9))
            check("9 the row says device", row9.get("form_captcha") == "device",
                  row9.get("form_captcha"))
            dev9 = row9.get("device") or {}
            check("9 the device trust was recorded",
                  dev9.get("ok") is True
                  and (dev9.get("signup_check") or {}).get("requires_captcha") is False,
                  dev9)
            check("9 one SMS and one write",
                  (M.Cfg.sends, M.Cfg.register_tries) == (1, 1),
                  (M.Cfg.sends, M.Cfg.register_tries))
            # The mock answers 400 "Unsupported captcha platform" to any non-token form
            # param from an untrusted device, so a 303 here *is* the empty-token proof:
            # had the device path been refused, the wrapper would have bought a second
            # token (cost 2) or the write would have failed.
            check("9 the form needed no second solve (303 with an empty form token)",
                  (row9.get("register") or {}).get("status") == 303,
                  (row9.get("register") or {}).get("status"))

    finally:
        if stop is not None:
            stop.set()
        if th is not None:
            th.join(timeout=2)
        d.terminate()
        try:
            d.wait(timeout=5)
        except Exception:
            d.kill()

    print("ALL READY TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
