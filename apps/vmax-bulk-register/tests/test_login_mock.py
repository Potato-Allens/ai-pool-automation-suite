# -*- coding: utf-8 -*-
"""vmax_login: verify accounts by password login (1 captcha token, no SMS).

Covers: register -> login round trip through the mock, wrong password (terminal),
missing credentials (terminal, costs nothing), resume skipping both, --save-token,
and a rejected captcha (403, retryable) that still spends its token.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import mock_e2e as M  # noqa: E402
import vmax_register as R  # noqa: E402

PY = sys.executable
fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def seed_tokens(d, n, param=None):
    for i in range(n):
        p = os.path.join(d, f"tok_{i:04d}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"param": M.Cfg.token if param is None else param}, f)
        os.utime(p, (time.time() + i, time.time() + i))


def left(d):
    return sum(1 for n in os.listdir(d) if n.startswith("tok_") and n.endswith(".json"))


def run_login(tmp, csv, out, *extra, timeout=180):
    return subprocess.run([PY, "vmax_login.py", "--input", csv, "--out", out,
                           "--captcha", "queue", "--queue-dir", os.path.join(tmp, "tokens"),
                           "--queue-wait", "5", "--base", M.Cfg.base,
                           "--redirect", M.Cfg.base + "/auth/callback", *extra],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="login_")
    # Isolate the register pacer: the toolkit's own .vmax_pace.json carries a real
    # ban window from live runs, which would make this mock test sleep ~45 s.
    os.environ["VMAX_PACE_STATE"] = os.path.join(tmp, "pace.json")
    os.environ["VMAX_RESEND_WAIT"] = "0"
    tok = os.path.join(tmp, "tokens")
    os.makedirs(tok, exist_ok=True)

    M.Cfg.cooldown = 0
    M.Cfg.reject_first = 0
    M.Cfg.rate_limit_after = 0
    M.Cfg.phone_taken = set()
    M.Cfg.send_code_fail = {}
    M.Cfg.users.clear()
    M.Cfg.login_tries = 0
    M.Cfg.sms_dir = os.path.join(tmp, "sms")
    os.makedirs(M.Cfg.sms_dir, exist_ok=True)
    M.Cfg.codes_path = os.path.join(tmp, "codes.jsonl")

    accounts = []
    with M.Server():
        print("[1] register three accounts, then log in with their credentials")
        R.build_sms = lambda spec, d="sms_inbox": M.LatestCodeSms()
        for phone in ("13500135051", "13500135052", "13500135053"):
            a = M.make_args()
            a.phone = phone
            rec = R.register_one(a)
            accounts.append({"phone": phone, "username": rec["username"],
                             "password": rec["password"]})
        check("three accounts stored by the mock", len(M.Cfg.users) == 3, list(M.Cfg.users))

        csv = os.path.join(tmp, "accounts.csv")
        with open(csv, "w", encoding="utf-8") as f:
            f.write("phone,username,password\n")
            # accounts[2] is deliberately listed once, with a wrong password: a
            # second row for the same phone would be collapsed by dedupe_accounts().
            for a in accounts[:2]:
                f.write(f"{a['phone']},{a['username']},{a['password']}\n")
            f.write(f"{accounts[2]['phone']},{accounts[2]['username']},definitely-wrong\n")
            f.write("13500135059,,\n")
        out = os.path.join(tmp, "logins.jsonl")
        seed_tokens(tok, 4)
        before = left(tok)
        r = run_login(tmp, csv, out)
        rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
        by_phone = {}
        for x in rows:
            by_phone.setdefault(x["phone"], []).append(x)
        ok_rows = [x for x in rows if x.get("status") == "ok"]
        kinds = {}
        for x in rows:
            if x.get("status") != "ok":
                kinds[x.get("kind")] = kinds.get(x.get("kind"), 0) + 1
        check("four rows", len(rows) == 4, len(rows))
        check("two ok", len(ok_rows) == 2, [x.get("status") for x in rows])
        check("uid on every ok row", all(x.get("uid") for x in ok_rows), ok_rows)
        check("phone masked by the server",
              all("*" in (x.get("phone_masked") or "") for x in ok_rows),
              [x.get("phone_masked") for x in ok_rows])
        check("bad password -> auth_failed", kinds.get("auth_failed") == 1, kinds)
        check("every distinct phone kept (no dedupe collapse)",
              sorted(by_phone) == sorted([a["phone"] for a in accounts] + ["13500135059"]),
              sorted(by_phone))
        check("one login attempt per row with credentials",
              M.Cfg.login_tries == 3, M.Cfg.login_tries)
        check("no credentials -> no_credentials", kinds.get("no_credentials") == 1, kinds)
        check("no token spent on the credential-less row", left(tok) == before - 3, left(tok))
        check("exit 1 (one real failure)", r.returncode == 1, r.returncode)
        check("access_token not stored by default",
              all("access_token" not in x for x in rows))
        check("token masked instead",
              all(x.get("token_masked", "").endswith("...") or "..." in x.get("token_masked", "")
                  for x in ok_rows), [x.get("token_masked") for x in ok_rows])
        check("expires_in ~3600s",
              all(3500 <= (x.get("expires_in") or 0) <= 3600 for x in ok_rows),
              [x.get("expires_in") for x in ok_rows])
        summ = json.load(open(out + ".summary.json", encoding="utf-8"))
        check("summary ok=2 fail=2", (summ["ok"], summ["fail"]) == (2, 2), summ)
        # live production order: credentials first -> the 401 row never consumed its token
        check("summary counts 2 consumed tokens", summ["captcha_tokens_spent"] == 2, summ)
        check("three tokens were popped", summ["tokens_popped"] == 3, summ)
        check("no reuse needed (the 401 was the last credential row)",
              summ["tokens_reused"] == 0, summ)

        print("[2] resume: ok and terminal rows are not retried")
        spent = left(tok)
        r = run_login(tmp, csv, out)
        check("resume exit 0", r.returncode == 0, r.returncode)
        check("nothing to check", "0 to check" in r.stdout, r.stdout[-300:])
        check("no token spent on resume", left(tok) == spent, left(tok))
        check("no new rows", len([l for l in open(out, encoding="utf-8") if l.strip()]) == 4)

        print("[3] --save-token stores a usable JWT")
        out2 = os.path.join(tmp, "logins2.jsonl")
        seed_tokens(tok, 1)
        r = run_login(tmp, csv, out2, "--save-token", "--limit", "1")
        rows2 = [json.loads(l) for l in open(out2, encoding="utf-8") if l.strip()]
        check("one row", len(rows2) == 1, len(rows2))
        tok3 = rows2[0].get("access_token") or ""
        check("token stored with three parts", tok3.count(".") == 2, tok3[:40])
        check("summary says tokens stored",
              json.load(open(out2 + ".summary.json", encoding="utf-8"))["tokens_stored"] is True)

        print("[4] a rejected captcha is retryable, but the token is gone")
        M.Cfg.token = "A-DIFFERENT-TOKEN"
        out3 = os.path.join(tmp, "logins3.jsonl")
        seed_tokens(tok, 1, "STALE-TOKEN")
        before = left(tok)
        r = run_login(tmp, csv, out3, "--limit", "1")
        row = json.loads(open(out3, encoding="utf-8").read().strip().splitlines()[0])
        check("kind is captcha_required", row.get("kind") == "captcha_required", row)
        check("token was spent anyway", left(tok) == before - 1, left(tok))
        check("exit 1", r.returncode == 1, r.returncode)
        M.Cfg.token = "MOCK-TOKEN"
        seed_tokens(tok, 1)
        r = run_login(tmp, csv, out3)
        check("resume retries the captcha row", "1 to check" in r.stdout or
              "to check" in r.stdout, r.stdout[-300:])

        print("[5] phone + password only: the server accepts the phone as username")
        csv2 = os.path.join(tmp, "phone_only.csv")
        with open(csv2, "w", encoding="utf-8") as f:
            f.write("phone,password\n")
            f.write(f"{accounts[0]['phone']},{accounts[0]['password']}\n")
            f.write(f"{accounts[1]['phone']},nope-not-the-password\n")
        out5 = os.path.join(tmp, "logins5.jsonl")
        seed_tokens(tok, 2)
        before = left(tok)
        r = run_login(tmp, csv2, out5)
        rows5 = [json.loads(l) for l in open(out5, encoding="utf-8") if l.strip()]
        ok5 = [x for x in rows5 if x.get("status") == "ok"]
        check("both rows attempted with 2 tokens", left(tok) == before - 2, left(tok))
        check("phone-as-username row logged in", len(ok5) == 1, rows5)
        check("marked username_used_phone",
              ok5 and ok5[0].get("username_used_phone") is True, ok5)
        check("uid still filled in", ok5 and ok5[0].get("uid"), ok5)
        bad5 = [x for x in rows5 if x.get("status") != "ok"]
        check("wrong password is still auth_failed",
              bad5 and bad5[0].get("kind") == "auth_failed", bad5)
        summ5 = json.load(open(out5 + ".summary.json", encoding="utf-8"))
        # both rows carry no username, so both attempts used the phone (the
        # counter is "attempts that relied on phone-as-username", not successes)
        check("summary counts phone_as_username=2",
              summ5.get("phone_as_username") == 2, summ5)

        print("[5b] a 401 leaves the token intact: later rows reuse it")
        csv2b = os.path.join(tmp, "reuse.csv")
        with open(csv2b, "w", encoding="utf-8") as f:
            f.write("phone,username,password\n")
            f.write(f"{accounts[2]['phone']},{accounts[2]['username']},wrong-one\n")
            f.write(f"{accounts[1]['phone']},{accounts[1]['username']},wrong-two\n")
            f.write(f"{accounts[0]['phone']},{accounts[0]['username']},{accounts[0]['password']}\n")
        out6 = os.path.join(tmp, "logins6.jsonl")
        seed_tokens(tok, 3)
        before = left(tok)
        r = run_login(tmp, csv2b, out6)
        rows6 = [json.loads(l) for l in open(out6, encoding="utf-8") if l.strip()]
        summ6 = json.load(open(out6 + ".summary.json", encoding="utf-8"))
        check("one ok, two auth_failed",
              (summ6["ok"], summ6["fail"]) == (1, 2), summ6)
        check("only ONE token left the queue", left(tok) == before - 1,
              (left(tok), before))
        check("one token consumed (the success)",
              summ6["captcha_tokens_spent"] == 1, summ6)
        check("two rows reused it", summ6["tokens_reused"] == 2, summ6)
        reused_rows = [x for x in rows6 if x.get("token_reused")]
        check("the reused rows are the second 401 and the success",
              len(reused_rows) == 2 and reused_rows[-1].get("status") == "ok",
              [(x.get("phone"), x.get("status"), x.get("token_reused")) for x in rows6])
        check("rows marked token_spent consistently",
              [x.get("token_spent") for x in rows6] == [False, False, True],
              [x.get("token_spent") for x in rows6])

        print("[5c] --token-reuse-seconds 0 disables the reuse")
        out7 = os.path.join(tmp, "logins7.jsonl")
        seed_tokens(tok, 3)
        before = left(tok)
        r = run_login(tmp, csv2b, out7, "--token-reuse-seconds", "0")
        summ7 = json.load(open(out7 + ".summary.json", encoding="utf-8"))
        check("three tokens popped with reuse off", left(tok) == before - 3, left(tok))
        check("summary says no reuse", summ7["tokens_reused"] == 0, summ7)
        check("still only one success", summ7["ok"] == 1, summ7)

        print("[6] an empty number pool fails before spending anything")
        out4 = os.path.join(tmp, "logins4.jsonl")
        empty = os.path.join(tmp, "empty.csv")
        with open(empty, "w", encoding="utf-8") as f:
            f.write("phone,username,password\n")
        r = run_login(tmp, empty, out4, timeout=60)
        check("exit 2 on an empty input", r.returncode == 2, r.returncode)
        check("nothing written", not os.path.exists(out4) or
              os.path.getsize(out4) == 0)

    print()
    if fails:
        print(f"FAILED {len(fails)}: " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
