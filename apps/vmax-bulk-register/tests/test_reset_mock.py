# -*- coding: utf-8 -*-
"""Password-reset recovery: what to do with a number that already has an account.

``/register/send-code`` refuses a taken number, and the runners treat that as
terminal (exit 7).  For a recycled SIM that throws the card away -- but the
service has the standard recovery flow, and it is *cheaper at the end*: step 2
(``POST /api/auth/reset-password``) needs no captcha at all and mints a JWT.

    [1] happy path: reset_one -> status ok / uid / JWT, new password really works
    [2] 404 not registered -> reset_error_kind=not_registered, zero SMS
    [3] wrong code          -> code_wrong (retry needs a fresh code)
    [4] captcha gate        -> incomplete / rejected, zero SMS
    [5] vmax_go --on-taken reset: 1 taken + 1 fresh -> exit 0, reused=1, both usable
    [6] control --on-taken skip : exit 7, taken=2, reused=0
    [7] daily cap (429)     -> vmax_reset exits 3 instead of grinding
    [8] resume              -> exit 4, nothing spent
    [9] go + cap            -> 2 strikes close the reset channel; fresh numbers still run
    [9b] 4 capped numbers   -> 2 tokens, not 4 (the breaker is bounded)
    [10] cap memo           -> a second run today skips it; --retry-limited forces
    [10b] load_limited      -> only today's cap counts, the latest row wins
    [11] batch + cap        -> same breaker, limited=2, no SMS
    [12] report             -> a failed reset is not a registration error
    [13] day verdict        -> exit 7 says the reset channel was capped
    [14] preflight          -> schema + one empty-captcha POST, 0 SMS; hard only with reset
    [14c] doctor --flows    -> the same two probes from the standalone preflight tool
    [15] rescue             -> --on-taken reset revives taken numbers, but a number that
                               answered 429 today waits for tomorrow (0 token)
    [15b] same day rerun    -> nothing touched, exit 7 (not a false "nothing to do")
    [15c] --retry-limited   -> forces today's capped ones anyway, breaker still 2 strikes
    [15d] next day          -> the same file is eligible again, both rescued
    [16] batch              -> the same rule through vmax_batch --workers 2
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import mock_e2e as M  # noqa: E402
import vmax_reset as R  # noqa: E402
from vmax_client import VmaxAuth  # noqa: E402

PY = sys.executable
TAKEN = "13500135051"
FRESH = "13500135052"
TAKEN2 = "13500135053"
UID_TAKEN = "taken001"
fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def reset_mock():
    M.Cfg.cooldown = 0
    M.Cfg.reject_first = 0
    M.Cfg.omit_code = False
    M.Cfg.rate_limit_after = 0
    M.Cfg.counter = 0
    M.Cfg.sends = 0
    M.Cfg.register_tries = 0
    M.Cfg.login_tries = 0
    M.Cfg.reset_rate_limited = False
    M.Cfg.reset_reject_first = 0
    M.Cfg.drop_reset_flow = False
    for d in (M.Cfg.codes, M.Cfg.users, M.Cfg.attempts, M.Cfg.last_send,
              M.Cfg.oauth, M.Cfg.send_code_fail):
        d.clear()
    M.Cfg.phone_taken.clear()


def seed_taken():
    M.Cfg.users[TAKEN] = {"username": "oldowner", "password": "OldPass12345",
                          "uid": UID_TAKEN}
    M.Cfg.users[TAKEN2] = {"username": "oldowner2", "password": "OldPass12345",
                           "uid": "taken002"}
    M.Cfg.phone_taken = {TAKEN, TAKEN2}


def make_args(tmp, **kw):
    ns = SimpleNamespace(
        phone=TAKEN, username=None, password=None, platform="dreammaker", invite="",
        captcha="param", captcha_param=M.Cfg.token, captcha_attempts=1,
        queue_dir=os.path.join(tmp, "tokens"), queue_wait=0,
        assist_dir=os.path.join(tmp, "assist"), stdin=False, headed=False,
        sms="file:local", sms_dir=os.path.join(tmp, "sms"), sms_timeout=10,
        sms_resends=0, proxy=None, form_captcha="auto",
        debug_dir=os.path.join(tmp, "dbg"), out=None,
        base=M.Cfg.base, redirect=M.Cfg.base + "/auth/callback", allow_virtual=False,
        on_taken="skip",
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def run(args, timeout=180):
    env = dict(os.environ, VMAX_RESEND_WAIT="0", PYTHONIOENCODING="utf-8",
               VMAX_PACE_STATE=os.path.join(TMP, "pace_%d.json" % int(time.time() * 1000)),
               VMAX_NOPACE="1")
    return subprocess.run([PY, "-u"] + args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def read_summary(path: str) -> dict:
    """A runner that bailed early writes no summary: report it, do not crash."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main() -> int:
    global TMP
    TMP = tempfile.mkdtemp(prefix="reset_")
    sms_dir = os.path.join(TMP, "sms")
    os.makedirs(sms_dir, exist_ok=True)
    M.Cfg.sms_dir = sms_dir
    M.Cfg.codes_path = os.path.join(TMP, "codes.jsonl")

    print("[0] classification vocabulary")
    cases = [
        ("手机号未注册", "not_registered"),
        ("发送频率或每日次数超限", "daily_limit"),
        ("验证码或新密码无效", "code_wrong"),
        ("Captcha verification rejected", "captcha_rejected"),
        ("该手机号已注册，请直接登录", "phone_taken"),
    ]
    for msg, want in cases:
        got = R.classify_reset_error(msg)
        check(f"{msg[:14]!r} -> {want}", got == want, got)

    with M.Server():
        reset_mock()
        seed_taken()
        # Built *after* Server() so base/redirect point at the mock, not production.
        args = make_args(TMP)

        print("[1] happy path: reset the password, keep the account")
        row = R.reset_one(args, TAKEN)
        check("status ok", row.get("status") == "ok", row)
        check("uid kept", row.get("uid") == UID_TAKEN, row.get("uid"))
        check("marked via=reset", row.get("via") == "reset", row.get("via"))
        check("new password stored", len(str(row.get("password") or "")) >= 8)
        check("JWT returned", bool((row.get("token") or {}).get("access_token")))
        check("one SMS spent", M.Cfg.sends == 1, M.Cfg.sends)
        cli = VmaxAuth(base=M.Cfg.base, redirect_uri=M.Cfg.base + "/auth/callback")
        lg = cli.s.post(f"{M.Cfg.base}/api/auth/login",
                        json={"username": TAKEN, "password": row["password"],
                              "captcha_verify_param": M.Cfg.token}, timeout=15)
        check("new password logs in", lg.status_code == 200, (lg.status_code, lg.text[:120]))
        old = cli.s.post(f"{M.Cfg.base}/api/auth/login",
                         json={"username": TAKEN, "password": "OldPass12345",
                               "captcha_verify_param": M.Cfg.token}, timeout=15)
        check("old password is gone", old.status_code == 401, old.status_code)

        print("[2] 404: the number has no account after all")
        before = M.Cfg.sends
        row = R.reset_one(make_args(TMP, phone="13900139999"), "13900139999")
        check("status error", row.get("status") == "error", row)
        check("bucket not_registered", row.get("reset_error_kind") == "not_registered",
              row.get("reset_error_kind"))
        check("zero SMS", M.Cfg.sends == before, M.Cfg.sends)
        check("http status kept", row.get("register_http_status") == 404,
              row.get("register_http_status"))

        print("[3] wrong SMS code")
        class _BadCode:
            def wait(self, phone, timeout=180, since=None):
                return "999999"
        real_build = R.build_sms
        R.build_sms = lambda spec, directory="sms_inbox": _BadCode()
        try:
            row = R.reset_one(args, TAKEN)
        finally:
            R.build_sms = real_build
        check("status error", row.get("status") == "error", row)
        check("bucket code_wrong", row.get("reset_error_kind") == "code_wrong",
              row.get("reset_error_kind"))
        check("old password still valid",
              M.Cfg.users[TAKEN]["password"] != row.get("password"))

        print("[4] captcha gate on the reset send-code")
        cli = VmaxAuth(base=M.Cfg.base, redirect_uri=M.Cfg.base + "/auth/callback")
        before = M.Cfg.sends
        r1 = cli.send_reset_code(TAKEN, "")
        check("empty param rejected", r1.get("ok") is False, r1)
        check("says incomplete", "Incomplete captcha" in str(r1.get("message")), r1)
        r2 = cli.send_reset_code(TAKEN, "junk")
        check("bogus token rejected", "Captcha verification rejected" in str(r2.get("message")), r2)
        check("still zero SMS", M.Cfg.sends == before, M.Cfg.sends)

        print("[5] vmax_go --on-taken reset: taken + fresh, one pass")
        reset_mock()
        seed_taken()
        csv2 = os.path.join(TMP, "two.csv")
        with open(csv2, "w", encoding="utf-8") as f:
            f.write("phone\n" + TAKEN + "\n" + FRESH + "\n")
        out2 = os.path.join(TMP, "go.jsonl")
        r = run([os.path.join(ROOT, "vmax_go.py"), "--input", csv2, "--out", out2,
                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                 "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "10",
                 "--on-taken", "reset", "--delay", "0", "--yes",
                 "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"])
        rows = read_jsonl(out2)
        summ = json.load(open(out2 + ".summary.json", encoding="utf-8"))
        check("exit 0", r.returncode == 0, (r.returncode, r.stdout[-600:]))
        check("summary ok=2", summ.get("ok") == 2, summ)
        check("summary reused=1", summ.get("reused") == 1, summ.get("reused"))
        check("summary taken=0", summ.get("taken") == 0, summ.get("taken"))
        check("three rows (taken, reset, fresh)", len(rows) == 3, [x.get("status") for x in rows])
        reset_rows = [x for x in rows if x.get("via") == "reset"]
        check("one reset row", len(reset_rows) == 1, reset_rows)
        check("reset row ok", reset_rows and reset_rows[0].get("status") == "ok", reset_rows)
        check("log says recovered", "recovered by password reset" in r.stdout, r.stdout[-400:])
        check("two SMS for two accounts", M.Cfg.sends == 2, M.Cfg.sends)

        print("[6] control: --on-taken skip leaves them taken")
        reset_mock()
        seed_taken()
        M.Cfg.users[FRESH] = {"username": "freshowner", "password": "FreshPass123",
                              "uid": "fresh01"}
        M.Cfg.phone_taken = {TAKEN, FRESH}
        out6 = os.path.join(TMP, "skip.jsonl")
        r = run([os.path.join(ROOT, "vmax_go.py"), "--input", csv2, "--out", out6,
                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                 "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                 "--on-taken", "skip", "--delay", "0", "--yes",
                 "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"])
        summ = json.load(open(out6 + ".summary.json", encoding="utf-8"))
        check("exit 7", r.returncode == 7, r.returncode)
        check("taken=2", summ.get("taken") == 2, summ.get("taken"))
        check("reused=0", summ.get("reused") == 0, summ.get("reused"))
        check("zero SMS spent", M.Cfg.sends == 0, M.Cfg.sends)

        print("[7] daily cap on the reset code -> vmax_reset exits 3")
        reset_mock()
        seed_taken()
        M.Cfg.reset_rate_limited = True
        taken_in = os.path.join(TMP, "taken.jsonl")
        with open(taken_in, "w", encoding="utf-8") as f:
            f.write(json.dumps({"phone": TAKEN, "status": "error",
                                "register_error_kind": "phone_taken",
                                "ts": int(time.time())}) + "\n")
        out7 = os.path.join(TMP, "reset7.jsonl")
        r = run([os.path.join(ROOT, "vmax_reset.py"), "--input", taken_in, "--out", out7,
                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                 "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                 "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"])
        rows7 = read_jsonl(out7)
        check("exit 3", r.returncode == 3, (r.returncode, r.stdout[-400:]))
        check("row says daily_limit",
              rows7 and rows7[0].get("reset_error_kind") == "daily_limit", rows7)
        check("no SMS", M.Cfg.sends == 0, M.Cfg.sends)

        print("[8] resume the successful run: nothing left to do")
        M.Cfg.reset_rate_limited = False
        n_before = len(read_jsonl(out2))
        sends_before = M.Cfg.sends
        r = run([os.path.join(ROOT, "vmax_go.py"), "--input", csv2, "--out", out2,
                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                 "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                 "--on-taken", "reset", "--delay", "0", "--yes",
                 "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"])
        check("exit 4 (nothing to do)", r.returncode == 4, (r.returncode, r.stdout[-300:]))
        check("no new rows", len(read_jsonl(out2)) == n_before, len(read_jsonl(out2)))
        check("no new SMS", M.Cfg.sends == sends_before, M.Cfg.sends)

        print("[9] vmax_go --on-taken reset: a capped channel stops burning tokens")
        reset_mock()
        seed_taken()
        M.Cfg.users[TAKEN2] = {"username": "oldowner2", "password": "OldPass12345",
                               "uid": "taken002"}
        M.Cfg.phone_taken = {TAKEN, TAKEN2}
        M.Cfg.reset_rate_limited = True
        csv9 = os.path.join(TMP, "two_taken.csv")
        with open(csv9, "w", encoding="utf-8") as f:
            f.write("phone\n" + TAKEN + "\n" + TAKEN2 + "\n" + FRESH + "\n")
        out9 = os.path.join(TMP, "go_limited.jsonl")
        r = run([os.path.join(ROOT, "vmax_go.py"), "--input", csv9, "--out", out9,
                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                 "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                 "--on-taken", "reset", "--delay", "0", "--yes",
                 "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"])
        rows9 = read_jsonl(out9)
        summ9 = json.load(open(out9 + ".summary.json", encoding="utf-8"))
        check("exit 7 (taken rows remain, the run itself finished)", r.returncode == 7,
              (r.returncode, r.stdout[-500:]))
        check("summary reset_limited", summ9.get("reset_limited") is True, summ9)
        check("summary ok=1 (the fresh number still got registered)",
              summ9.get("ok") == 1, summ9.get("ok"))
        check("summary taken=2", summ9.get("taken") == 2, summ9.get("taken"))
        check("two reset attempts (the breaker needs 2 strikes)",
              len([x for x in rows9 if x.get("via") == "reset"]) == 2,
              [x.get("via") for x in rows9])
        check("one SMS (the fresh registration only)", M.Cfg.sends == 1, M.Cfg.sends)
        check("log explains the breaker", "no more resets this run" in r.stdout,
              r.stdout[-500:])

        print("[9b] four capped numbers cost two tokens, not four")
        reset_mock()
        seed_taken()
        more = ["13500135054", "13500135055"]
        for k, ph in enumerate(more):
            M.Cfg.users[ph] = {"username": "old%d" % k, "password": "OldPass12345",
                               "uid": "t9b%d" % k}
        M.Cfg.phone_taken = {TAKEN, TAKEN2, more[0], more[1]}
        M.Cfg.reset_rate_limited = True
        csv9b = os.path.join(TMP, "four_taken.csv")
        with open(csv9b, "w", encoding="utf-8") as f:
            f.write("phone\n" + "\n".join([TAKEN, TAKEN2] + more) + "\n")
        out9b = os.path.join(TMP, "go_four.jsonl")
        r = run([os.path.join(ROOT, "vmax_go.py"), "--input", csv9b, "--out", out9b,
                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                 "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                 "--on-taken", "reset", "--delay", "0", "--yes",
                 "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"])
        rows9b = read_jsonl(out9b)
        summ9b = json.load(open(out9b + ".summary.json", encoding="utf-8"))
        check("exit 7", r.returncode == 7, r.returncode)
        check("reset attempts == 2 (not 4)",
              len([x for x in rows9b if x.get("via") == "reset"]) == 2,
              [x.get("via") for x in rows9b])
        check("taken=4", summ9b.get("taken") == 4, summ9b.get("taken"))
        check("zero SMS", M.Cfg.sends == 0, M.Cfg.sends)

        print("[10] vmax_reset: a phone capped today is skipped, --retry-limited forces it")
        M.Cfg.reset_rate_limited = True

        def reset_cmd(inp, outp, extra=()):
            return [os.path.join(ROOT, "vmax_reset.py"), "--input", inp, "--out", outp,
                    "--captcha", "param", "--captcha-param", M.Cfg.token,
                    "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                    "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"] \
                   + list(extra)

        now_ts = int(time.time())
        in10 = os.path.join(TMP, "taken10.jsonl")
        with open(in10, "w", encoding="utf-8") as f:
            for ph in (TAKEN, TAKEN2):
                f.write(json.dumps({"phone": ph, "status": "error",
                                    "register_error_kind": "phone_taken",
                                    "ts": now_ts}) + "\n")
            f.write(json.dumps({"phone": TAKEN, "status": "error",
                                "reset_error_kind": "daily_limit",
                                "ts": now_ts}) + "\n")
        out10 = os.path.join(TMP, "reset10.jsonl")
        r = run(reset_cmd(in10, out10))
        rows10 = read_jsonl(out10)
        check("exit 3 (the other phone is capped too)", r.returncode == 3,
              (r.returncode, r.stdout[-400:]))
        check("only the un-capped phone was tried",
              [x.get("phone") for x in rows10] == [TAKEN2], rows10)
        check("log says skipped", "already hit today's cap" in r.stdout, r.stdout[-400:])
        check("zero SMS", M.Cfg.sends == 0, M.Cfg.sends)
        out10b = os.path.join(TMP, "reset10b.jsonl")
        r = run(reset_cmd(in10, out10b, ["--retry-limited"]))
        rows10b = read_jsonl(out10b)
        check("--retry-limited tries the capped phone",
              [x.get("phone") for x in rows10b] == [TAKEN], rows10b)
        check("and still stops at the first 429", r.returncode == 3, r.returncode)

        print("[10b] load_limited: only today's cap counts, the latest row wins")
        memo = os.path.join(TMP, "memo.jsonl")
        with open(memo, "w", encoding="utf-8") as f:
            f.write(json.dumps({"phone": TAKEN, "status": "error",
                                "reset_error_kind": "daily_limit",
                                "ts": now_ts}) + "\n")
            f.write(json.dumps({"phone": TAKEN2, "status": "error",
                                "reset_error_kind": "daily_limit",
                                "ts": now_ts - 86400}) + "\n")
        lim = R.load_limited(memo)
        check("today's cap remembered", TAKEN in lim, lim)
        check("yesterday's cap forgotten", TAKEN2 not in lim, lim)
        with open(memo, "a", encoding="utf-8") as f:
            f.write(json.dumps({"phone": TAKEN2, "status": "ok", "via": "reset",
                                "ts": now_ts}) + "\n")
        lim2 = R.load_limited(memo)
        check("an ok row clears the memo", TAKEN2 not in lim2, lim2)

        print("[11] vmax_batch --on-taken reset: same breaker across the queue")
        reset_mock()
        seed_taken()
        for k, ph in enumerate(more):
            M.Cfg.users[ph] = {"username": "old%d" % k, "password": "OldPass12345",
                               "uid": "t9b%d" % k}
        M.Cfg.phone_taken = {TAKEN, TAKEN2, more[0], more[1]}
        M.Cfg.reset_rate_limited = True
        out11 = os.path.join(TMP, "batch11.jsonl")
        r = run([os.path.join(ROOT, "vmax_batch.py"), "--input", csv9b, "--out", out11,
                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                 "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                 "--on-taken", "reset", "--delay", "0", "--workers", "1",
                 "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"])
        rows11 = read_jsonl(out11)
        check("exit 7", r.returncode == 7, (r.returncode, r.stdout[-600:]))
        check("summary line has limited=2", "limited=2" in r.stdout, r.stdout[-400:])
        check("reset attempts == 2 (not 4)",
              len([x for x in rows11 if x.get("via") == "reset"]) == 2,
              [x.get("via") for x in rows11])
        check("zero SMS", M.Cfg.sends == 0, M.Cfg.sends)

        print("[12] report: a failed reset is not a registration error")
        rep = os.path.join(TMP, "rep12.jsonl")
        with open(rep, "w", encoding="utf-8") as f:
            f.write(json.dumps({"phone": FRESH, "status": "ok", "uid": "u1",
                                "ts": now_ts}) + "\n")
            f.write(json.dumps({"phone": TAKEN, "status": "error",
                                "register_error_kind": "phone_taken",
                                "ts": now_ts}) + "\n")
            f.write(json.dumps({"phone": TAKEN, "status": "error",
                                "reset_error_kind": "daily_limit",
                                "error": "VmaxError: send-code rejected: 429 \u53d1\u9001\u9891\u7387\u6216\u6bcf\u65e5\u6b21\u6570\u8d85\u9650",
                                "ts": now_ts}) + "\n")
        r = run([os.path.join(ROOT, "vmax_report.py"), rep, "--json"])
        rj = json.loads(r.stdout)
        check("error=0", rj.get("error") == 0, rj)
        check("taken=1", rj.get("taken") == 1, rj)
        check("reset_failed=1", rj.get("reset_failed") == 1, rj)
        check("limited=1", rj.get("limited") == 1, rj)
        check("attempted counts the ok row only", rj.get("attempted") == 1, rj)
        check("success_rate 1.0", rj.get("success_rate") == 1.0, rj)

        print("[13] day verdict names a capped reset channel")
        import vmax_day as D
        check("plain 429", D.verdict_for(3, {"aborted_on_429": True}) == "aborted_429",
              D.verdict_for(3, {"aborted_on_429": True}))
        check("capped resets",
              D.verdict_for(7, {"reset_limited": True}) == "pool_had_taken_reset_capped",
              D.verdict_for(7, {"reset_limited": True}))
        check("or skipped from today's memo",
              D.verdict_for(7, {"skipped_capped": 2}) == "pool_had_taken_reset_capped",
              D.verdict_for(7, {"skipped_capped": 2}))
        check("plain taken pool", D.verdict_for(7, {}) == "pool_had_taken",
              D.verdict_for(7, {}))
        check("plain ok", D.verdict_for(0, {}) == "ok", D.verdict_for(0, {}))
        check("unknown code kept", D.verdict_for(12, None) == "batch_exit_12",
              D.verdict_for(12, None))
        # 9 stopped being "unknown" in R115: it now names the receiver, so a
        # dead SMS channel never reads as a mysterious batch failure.
        check("exit 9 means the SMS channel, not the batch",
              D.verdict_for(9, None) == "sms_channel_down", D.verdict_for(9, None))

        print("[14] preflight with --on-taken reset probes the recovery flow (0 SMS)")
        reset_mock()
        seed_taken()

        def preflight_cmd(extra=()):
            return [os.path.join(ROOT, "vmax_go.py"), "--preflight-only",
                    "--captcha", "param", "--captcha-param", M.Cfg.token,
                    "--queue-dir", os.path.join(TMP, "tokens"),
                    "--base", M.Cfg.base,
                    "--redirect", M.Cfg.base + "/auth/callback"] + list(extra)

        r = run(preflight_cmd(["--on-taken", "reset"]))
        check("exit 0", r.returncode == 0, (r.returncode, r.stdout[-600:]))
        check("schema check is green", "[OK  ] reset_schema" in r.stdout, r.stdout[-500:])
        check("live probe is green", "[OK  ] reset_probe" in r.stdout, r.stdout[-500:])
        check("probe spent no SMS", M.Cfg.sends == 0, M.Cfg.sends)

        print("[14b] a site that drops the reset routes: hard with reset, soft with skip")
        M.Cfg.drop_reset_flow = True
        r = run(preflight_cmd(["--on-taken", "reset"]))
        check("exit 2 (hard check failed)", r.returncode == 2, (r.returncode, r.stdout[-500:]))
        check("the missing route is named",
              "send-code=False" in r.stdout, r.stdout[-500:])
        check("probe skipped when the schema is wrong",
              "reset_probe" not in r.stdout, r.stdout[-500:])
        check("still zero SMS", M.Cfg.sends == 0, M.Cfg.sends)
        r = run(preflight_cmd(["--on-taken", "skip"]))
        check("soft with --on-taken skip (exit 0)", r.returncode == 0,
              (r.returncode, r.stdout[-400:]))
        M.Cfg.drop_reset_flow = False

        print("[14c] vmax_doctor --flows does the same two probes")
        r = run([os.path.join(ROOT, "vmax_doctor.py"), "--flows", "--skip-egress",
                 "--min-paths", "1", "--queue-dir", os.path.join(TMP, "tokens"),
                 "--base", M.Cfg.base,
                 "--redirect", M.Cfg.base + "/auth/callback"])
        check("exit 0", r.returncode == 0, (r.returncode, r.stdout[-600:]))
        check("doctor schema green", "[OK ] reset_schema" in r.stdout, r.stdout[-500:])
        check("doctor probe green", "[OK ] reset_probe" in r.stdout, r.stdout[-500:])
        check("doctor spent no SMS", M.Cfg.sends == 0, M.Cfg.sends)

        print("[15] --on-taken reset revives taken numbers -- but not on the day they capped")
        reset_mock()
        seed_taken()
        TAKEN3 = "13500135056"
        M.Cfg.users[TAKEN3] = {"username": "old3", "password": "OldPass12345",
                               "uid": "taken003"}
        M.Cfg.phone_taken = {TAKEN, TAKEN2, TAKEN3}
        csv15 = os.path.join(TMP, "rescue.csv")
        with open(csv15, "w", encoding="utf-8") as f:
            f.write("phone\n" + "\n".join([TAKEN, TAKEN2, TAKEN3]) + "\n")
        out15 = os.path.join(TMP, "rescue.jsonl")
        now15 = int(time.time())
        with open(out15, "w", encoding="utf-8") as f:
            for ph in (TAKEN, TAKEN2):
                f.write(json.dumps({"phone": ph, "status": "error",
                                    "register_error_kind": "phone_taken",
                                    "ts": now15}) + "\n")
            # found taken *yesterday* -> eligible again today
            f.write(json.dumps({"phone": TAKEN3, "status": "error",
                                "register_error_kind": "phone_taken",
                                "ts": now15 - 86400}) + "\n")
            for ph in (TAKEN, TAKEN2):
                f.write(json.dumps({"phone": ph, "status": "error",
                                    "reset_error_kind": "daily_limit",
                                    "ts": now15}) + "\n")

        def go_cmd(inp, outp, extra=()):
            return [os.path.join(ROOT, "vmax_go.py"), "--input", inp, "--out", outp,
                    "--captcha", "param", "--captcha-param", M.Cfg.token,
                    "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                    "--on-taken", "reset", "--delay", "0", "--yes",
                    "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"] \
                   + list(extra)

        sends15 = M.Cfg.sends
        r = run(go_cmd(csv15, out15))
        s15 = read_summary(out15 + ".summary.json")
        new15 = [x for x in read_jsonl(out15) if x.get("via") == "reset"]
        check("exit 7 (two taken numbers still wait)", r.returncode == 7,
              (r.returncode, r.stdout[-600:]))
        check("only the un-capped taken number was touched",
              [x.get("phone") for x in new15] == [TAKEN3], new15)
        check("and it was recovered", bool(new15) and new15[0].get("status") == "ok",
              new15)
        check("summary skipped_capped=2", s15.get("skipped_capped") == 2, s15)
        check("summary keeps the breaker honest (it never fired)",
              s15.get("reset_limited") is False, s15)
        check("summary planned=1", s15.get("planned") == 1, s15)
        check("one SMS (the rescue), not three", M.Cfg.sends - sends15 == 1,
              M.Cfg.sends - sends15)
        check("log says why they wait", "hit today's reset cap" in r.stdout,
              r.stdout[-700:])

        print("[15b] second run the same day: nothing touched, still exit 7")
        n15 = len(read_jsonl(out15))
        sends15 = M.Cfg.sends
        r = run(go_cmd(csv15, out15))
        s15b = read_summary(out15 + ".summary.json")
        check("exit 7 (not 4: the capped numbers still need a later run)",
              r.returncode == 7, (r.returncode, r.stdout[-600:]))
        check("no new rows", len(read_jsonl(out15)) == n15, len(read_jsonl(out15)))
        check("zero SMS", M.Cfg.sends == sends15, M.Cfg.sends)
        check("summary skipped_capped=2", s15b.get("skipped_capped") == 2, s15b)

        print("[15c] --retry-limited forces today's capped numbers anyway")
        M.Cfg.reset_rate_limited = True
        sends15 = M.Cfg.sends
        r = run(go_cmd(csv15, out15, ["--retry-limited"]))
        rows15c = [x for x in read_jsonl(out15)[n15:] if x.get("via") == "reset"]
        check("exit 7", r.returncode == 7, (r.returncode, r.stdout[-500:]))
        check("two reset attempts (the breaker, not four)", len(rows15c) == 2, rows15c)
        check("zero SMS", M.Cfg.sends == sends15, M.Cfg.sends)

        print("[15d] the next day the same file is eligible again")
        M.Cfg.reset_rate_limited = False
        moved = []
        for x in read_jsonl(out15):
            # a whole new day: every row is now dated yesterday
            if x.get("ts"):
                x["ts"] = int(x["ts"]) - 86400
            moved.append(x)
        with open(out15, "w", encoding="utf-8") as f:
            for x in moved:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        sends15 = M.Cfg.sends
        r = run(go_cmd(csv15, out15))
        s15d = read_summary(out15 + ".summary.json")
        check("exit 0 (both were rescued)", r.returncode == 0,
              (r.returncode, r.stdout[-800:]))
        check("summary reused=2", s15d.get("reused") == 2, s15d)
        check("summary skipped_capped=0", s15d.get("skipped_capped") == 0, s15d)
        check("two SMS", M.Cfg.sends - sends15 == 2, M.Cfg.sends - sends15)

        print("[16] vmax_batch: the same 0-token rule for today's capped numbers")
        reset_mock()
        seed_taken()
        M.Cfg.users[TAKEN3] = {"username": "old3", "password": "OldPass12345",
                               "uid": "taken003"}
        M.Cfg.phone_taken = {TAKEN, TAKEN2, TAKEN3}
        out16 = os.path.join(TMP, "batch16.jsonl")
        now16 = int(time.time())
        with open(out16, "w", encoding="utf-8") as f:
            for ph in (TAKEN, TAKEN2):
                f.write(json.dumps({"phone": ph, "status": "error",
                                    "register_error_kind": "phone_taken",
                                    "ts": now16}) + "\n")
            f.write(json.dumps({"phone": TAKEN3, "status": "error",
                                "register_error_kind": "phone_taken",
                                "ts": now16 - 86400}) + "\n")
            for ph in (TAKEN, TAKEN2):
                f.write(json.dumps({"phone": ph, "status": "error",
                                    "reset_error_kind": "daily_limit",
                                    "ts": now16}) + "\n")
        sends16 = M.Cfg.sends
        r = run([os.path.join(ROOT, "vmax_batch.py"), "--input", csv15, "--out", out16,
                 "--workers", "2", "--delay", "0",
                 "--captcha", "param", "--captcha-param", M.Cfg.token,
                 "--sms", "file:local", "--sms-dir", sms_dir, "--sms-timeout", "5",
                 "--on-taken", "reset",
                 "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback"])
        rows16 = [x for x in read_jsonl(out16) if x.get("via") == "reset"]
        check("exit 7", r.returncode == 7, (r.returncode, r.stdout[-700:]))
        check("summary counts the capped skips", "capped_skipped=2" in r.stdout,
              r.stdout[-500:])
        check("only the un-capped number was recovered",
              [x.get("phone") for x in rows16] == [TAKEN3], rows16)
        check("one SMS", M.Cfg.sends - sends16 == 1, M.Cfg.sends - sends16)

    print("ALL RESET TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
