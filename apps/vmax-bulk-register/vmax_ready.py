# -*- coding: utf-8 -*-
"""Day-one readiness: one real card, six criteria, one verdict.

    python vmax_ready.py --input cards.csv `
        --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=<TEST_SECRET>&since={since}" `
        --smsd http://127.0.0.1:8787 --secret <TEST_SECRET> `
        --out runs\ready

Why this exists
---------------
Everything before this point is offline or 0-SMS: the mock proves the client,
the probes prove the endpoints, the gates prove nothing is spent on a dead
channel.  The one thing that cannot be faked is "the carrier delivered this code
to this SIM and the platform accepted it".  This command is that single step,
wrapped so the answer is a verdict instead of a wall of logs.

What it runs, in order
----------------------
1. ``channel_check()`` -- the receiver answers?  (0 token, 0 SMS; exit 9 if not)
2. ``vmax_accept --smsd ... --smsd-selftest`` -- platform + inbox + modem state,
   plus a synthetic SMS to 13000000000 to prove phone -> PC -> tool.  No real card
   is touched.  Skipped with ``--no-accept`` / no ``--smsd``.
3. ``vmax_day`` on a one-row card file: farm -> one registration -> login verify
   -> report (1 captcha token, 1 real SMS, 1 /register write, 1 login).

Then it re-reads the artifacts and judges six wire criteria, two provenance
criteria and three soft ones, so "READY" is computed from the bytes on disk,
never from a process exiting 0.

    READY     0   six wire criteria + two provenance criteria all true
    NOT_READY 1   a criterion failed (see ready.json / READY.md for which)
    ARGS      2   bad input, or --out already holds rows from an earlier run
    BANNED    3   the /register write bucket answered 429 -- wait >=200 s, then rerun
    SMS_DOWN  9   the receiver is down: 0 token, 0 SMS, nothing was written

Provenance is not pedantry.  Reusing one ``--out`` directory used to be enough to
get READY for free: ``vmax_day`` finds the number already done, exits 4 ("nothing
to do"), and the *previous* run's 303 / token / uid / login row were still on
disk for the judge to read.  Measured (R139): second run, 0 SMS, 0 tokens,
verdict READY.  So the two provenance criteria are

    row_fresh     the row did not come back from the write-ahead journal
                  (``via: "journal"`` / ``journal_recovered``) -- that is a
                  crashed run's leftover, not a card this run just proved;
    sms_this_run  the row carries ``send_code.ok`` -- an SMS really went out.

and the run only judges the rows *it* appended (offsets are taken before
``vmax_day`` starts).  A dirty ``--out`` is refused (exit 2, ``dirty_out``)
unless ``--resume`` is given, and even then a recovered row cannot reach READY.

``--dry-run`` prints the three commands (secrets masked) and touches nothing.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import vmax_formcost as formcost  # noqa: E402
from vmax_console import utf8_stdout  # noqa: E402
from vmax_sms import channel_check  # noqa: E402

utf8_stdout()

PY = sys.executable
PHONE_COLS = ("phone", "mobile", "手机号", "手机", "号码")
SECRET_RE = re.compile(r"(secret|token|password|key)=([^&\s]+)", re.I)
# `--secret <TEST_SECRET>` is a different shape than `secret=<TEST_SECRET>`: the first one
# shows up in every printed command line, so it needs its own pattern.
FLAG_SECRET_RE = re.compile(r"(?i)(--(?:secret|token|password|key)\s+)(\S+)")

DONE = "\u2713"
FAIL = "\u2717"

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def mask(text: str) -> str:
    """Never print a secret, even in a command line we are about to run."""
    text = SECRET_RE.sub(lambda m: m.group(1) + "=***", text or "")
    return FLAG_SECRET_RE.sub(lambda m: m.group(1) + "***", text)


def _tool(name: str) -> list[str]:
    return [PY, "-u", os.path.join(HERE, name)]


def first_card(path: str) -> dict | None:
    """First row of a cards CSV/JSONL, as {phone, username, password, proxy}."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        head = f.read(4096)
        f.seek(0)
        if path.lower().endswith(".jsonl") or head.lstrip().startswith("{"):
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    row = json.loads(line)
                    return {k: (row.get(k) or "") for k in
                            ("phone", "username", "password", "proxy")} | {"phone": row.get("phone", "")}
        if "," in (head.splitlines()[0] if head.splitlines() else ""):
            for row in csv.DictReader(f):
                low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                phone = next((low[c] for c in PHONE_COLS if low.get(c)), "")
                if phone:
                    return {"phone": phone, "username": low.get("username", ""),
                            "password": low.get("password", ""), "proxy": low.get("proxy", "")}
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if any(ch.isdigit() for ch in line):
                return {"phone": re.sub(r"[^\d]", "", line.split(",")[0]),
                        "username": "", "password": "", "proxy": ""}
    return None


def run_live(cmd: list[str], log_path: str) -> tuple[int, float]:
    """Run a child, echo it live and keep the raw log. Returns (exit, seconds)."""
    print("[ready] $ " + mask(" ".join(cmd)), flush=True)
    t0 = time.time()
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             bufsize=1, env=env)
        for line in p.stdout:
            log.write(line)
            log.flush()
            print("    " + line.rstrip("\n"), flush=True)
        rc = p.wait()
    return rc, round(time.time() - t0, 1)


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows

def judge(results: list[dict], logins: list[dict]) -> dict:
    """Turn the artifacts into six hard criteria + the real cost of the run."""
    row = results[0] if results else {}
    reg = row.get("register") or {}
    tok = row.get("token") or {}
    loc = reg.get("location") or ""
    phone = row.get("phone")
    login = next((r for r in logins if r.get("phone") == phone), {})
    m = re.search(r"[?&]code=([^&]+)", loc)

    checks = [
        ("register_303", reg.get("status") == 303,
         "status=%s" % reg.get("status")),
        ("location_code", bool(m),
         ("code=" + (m.group(1)[:10] + "...")) if m else (loc[:90] or "no Location header")),
        ("location_state", bool(re.search(r"[?&]state=", loc)),
         "state echoed" if re.search(r"[?&]state=", loc) else "state missing from Location"),
        ("access_token", bool(tok.get("access_token")),
         "token_type=%s expires_in=%s" % (tok.get("token_type"), tok.get("expires_in"))),
        ("uid", bool(row.get("uid")), "uid=%s" % row.get("uid")),
        ("login_ok", login.get("status") == "ok",
         "login status=%s%s" % (login.get("status"),
                                "" if login.get("status") == "ok"
                                else " (%s)" % str(login.get("error") or login.get("login_error_kind") or "")[:80])),
    ]
    # Provenance: the six wire checks above say "a row looks like success".  These
    # two say "it became true *in this run, with a real SMS*".  A journal-recovered
    # row passes several wire checks by construction (it is a rebuilt row), so
    # without them a crashed run could be replayed as an acceptance.
    prov = [
        ("row_fresh", row.get("via") != "journal",
         "via=%s%s" % (row.get("via") or "live",
                       " (rebuilt from the write-ahead log)" if row.get("via") == "journal" else "")),
        ("sms_this_run", bool((row.get("send_code") or {}).get("ok")),
         "send_code=%s%s" % ((row.get("send_code") or {}).get("ok"),
                             " (0 SMS: a recovered row cannot be day-one evidence)"
                             if not (row.get("send_code") or {}).get("ok") else "")),
    ]
    soft = [
        ("one_sms", bool((row.get("send_code") or {}).get("ok")) and not (row.get("resends") or []),
         "send_code=%s resends=%d" % ((row.get("send_code") or {}).get("ok"),
                                      len(row.get("resends") or []))),
        # R152: one price list for the whole toolkit (vmax_formcost); the old
        # local whitelist is exactly what broke in R151.
        ("captcha_reused", formcost.is_free(row.get("form_captcha")),
         "form_captcha=%s" % (row.get("form_captcha")
                              or "single token (the form did not need a re-solve)")),
        ("no_429", row.get("register_error_kind") != "rate_limited",
         "register_error_kind=%s" % (row.get("register_error_kind") or "none")),
    ]
    # send-code always needs one; the form only when the token was burnt (fresh /
    # auto_retry) or the device path was refused (device_fallback / device_retry).
    kind = formcost.path(row)
    extra = formcost.extra_tokens(kind)
    if extra is None:     # an unknown label: never count it as a free ride
        extra = 0
    tokens = 0 if not row else 1 + extra
    sms = 0 if not row else (1 + len(row.get("resends") or [])) if (row.get("send_code") or {}).get("ok") else 0
    return {"phone": phone, "checks": [dict(name=n, ok=bool(o), detail=d) for n, o, d in checks],
            "provenance": [dict(name=n, ok=bool(o), detail=d) for n, o, d in prov],
            "soft": [dict(name=n, ok=bool(o), detail=d) for n, o, d in soft],
            "ready": all(o for _, o, _ in checks) and all(o for _, o, _ in prov),
            "form_captcha": kind,
            "cost": {"captcha_tokens": tokens, "form_extra_tokens": extra, "sms": sms,
                     "register_writes": 1 if reg.get("status") else 0,
                     "logins": 1 if login else 0},
            "row": row, "login": login}


def write_report(out_dir: str, verdict: dict) -> tuple[str, str]:
    """ready.json (machine) + READY.md (a table you can paste into chat)."""
    jpath = os.path.join(out_dir, "ready.json")
    mpath = os.path.join(out_dir, "READY.md")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=1)

    label = {"ready": "READY", "not_ready": "NOT_READY",
             "sms_channel_down": "SMS_CHANNEL_DOWN", "banned": "BANNED"}.get(
                 verdict["verdict"], str(verdict["verdict"]).upper())
    lines = ["# 实卡验收报告 · %s" % verdict["stamp"], "",
             "**判定：%s** · %s" % (label, verdict["headline"]), ""]
    if verdict.get("phone"):
        lines += ["- 卡号：`%s`" % verdict["phone"], "- 基址：%s" % verdict["base"], ""]
    lines += ["| # | 判据 | 结果 | 证据 |", "|---|---|---|---|"]
    for i, c in enumerate(verdict["checks"], 1):
        lines.append("| %d | `%s` | %s | %s |"
                     % (i, c["name"], "ok" if c["ok"] else "**FAIL**",
                        str(c["detail"]).replace("|", "\\|")))
    if verdict.get("provenance"):
        lines += ["", "出处判据（这行必须是**本轮**真短信换来的，不认上一轮、不认日志恢复）：", "",
                  "| 判据 | 结果 | 证据 |", "|---|---|---|"]
        for c in verdict["provenance"]:
            lines.append("| `%s` | %s | %s |"
                         % (c["name"], "ok" if c["ok"] else "**FAIL**",
                            str(c["detail"]).replace("|", "\\|")))
    if verdict["soft"]:
        lines += ["", "软判据（提示，不挡判定）：", "",
                  "| 判据 | 结果 | 证据 |", "|---|---|---|"]
        for c in verdict["soft"]:
            lines.append("| `%s` | %s | %s |" % (c["name"], "ok" if c["ok"] else "warn",
                                                 str(c["detail"]).replace("|", "\\|")))
    cost = verdict.get("cost") or {}
    lines += ["", "本次花费：captcha token **%s**、真实短信 **%s** 条、`/register` 写 **%s** 次。"
              % (cost.get("captcha_tokens"), cost.get("sms"), cost.get("register_writes"))]
    if verdict.get("next"):
        lines += ["", "下一步：", "", "```powershell", verdict["next"], "```"]
    lines.append("")
    with open(mpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return jpath, mpath

DEFAULT_SMS = "http://127.0.0.1:8787/code?phone={phone}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="day-one readiness: one card -> six criteria -> READY / NOT_READY")
    ap.add_argument("--input", help="cards.csv/.jsonl; only the first row is spent")
    ap.add_argument("--out", default=os.path.join("runs", "ready"),
                    help="run directory; a day-one acceptance needs a clean one "
                         "(existing rows => exit 2 unless --resume)")
    ap.add_argument("--sms", default=None,
                    help="receiver template (default: the local relay %s)" % DEFAULT_SMS)
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-timeout", type=int, default=300)
    ap.add_argument("--sms-resends", type=int, default=1,
                    help="how many times a burnt code may be re-sent (each costs 1 SMS)")
    ap.add_argument("--smsd", default=None,
                    help="inbox base URL for the accept gate, e.g. http://127.0.0.1:8787")
    ap.add_argument("--secret", default=os.environ.get("SMSD_SECRET", ""))
    ap.add_argument("--no-accept", action="store_true", help="skip the accept gate")
    ap.add_argument("--no-sms-check", action="store_true", help="skip the pre-token probe")
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--captcha", default="queue", choices=["queue", "param"])
    ap.add_argument("--form-captcha", default="auto",
                    choices=["auto", "reuse", "fresh", "device"],
                    help="device = the site's own signup-check path: the /register submit "
                         "needs no token (send-code still does); see RUNBOOK 3.1.0")
    ap.add_argument("--captcha-param", default="")
    ap.add_argument("--token-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=600)
    ap.add_argument("--farm-workers", type=int, default=2)
    ap.add_argument("--skip-farm", action="store_true")
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--on-taken", default="skip", choices=["skip", "reset"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--contract", default=None, metavar="BASELINE",
                    help="hand vmax_day.py a contract baseline: drift stops the run "
                         "before the first token or SMS is spent")
    ap.add_argument("--yes", action="store_true", help="accepted for parity; never prompts")
    ap.add_argument("--resume", action="store_true",
                    help="allow a non-empty --out: judge only the rows this run appends "
                         "(a dirty dir is refused without this)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if not a.input:
        print("[ready] --input is required (cards.csv); --dry-run prints the plan", file=sys.stderr)
        return 2
    if not os.path.isfile(a.input):
        print("[ready] input not found: %s" % a.input, file=sys.stderr)
        return 2
    card = first_card(a.input)
    if not card or not card.get("phone"):
        print("[ready] no phone found in the first row of %s" % a.input, file=sys.stderr)
        return 2

    out_dir = os.path.abspath(a.out)
    logs = os.path.join(out_dir, "logs")
    card_csv = os.path.join(out_dir, "card1.csv")

    sms = a.sms or DEFAULT_SMS
    base = a.base or os.environ.get("VMAX_BASE") or "https://auth.vobilemax.cn"
    common = []
    for flag, val in (("--base", a.base), ("--redirect", a.redirect), ("--proxy", a.proxy)):
        if val:
            common += [flag, val]
    if a.platform != "dreammaker":
        common += ["--platform", a.platform]

    day_cmd = _tool("vmax_day.py") + [
        "--input", card_csv, "--out", os.path.join(out_dir, "results.jsonl"),
        "--sms", sms, "--sms-dir", a.sms_dir, "--sms-timeout", str(a.sms_timeout),
        "--sms-resends", str(a.sms_resends), "--captcha", a.captcha,
        "--token-dir", a.token_dir, "--queue-wait", str(a.queue_wait),
        "--farm-workers", str(a.farm_workers), "--on-taken", a.on_taken,
        "--no-sms-check", "--yes"] + common
    if a.captcha == "param":
        day_cmd += ["--captcha-param", a.captcha_param]
    if getattr(a, "form_captcha", "auto") != "auto":
        day_cmd += ["--form-captcha", a.form_captcha]
    if a.skip_farm:
        day_cmd += ["--skip-farm"]
    if a.skip_preflight:
        day_cmd += ["--skip-preflight"]
    if a.force:
        day_cmd += ["--force"]
    if a.contract:
        day_cmd += ["--contract", os.path.abspath(a.contract)]

    accept_cmd = None
    if a.smsd and not a.no_accept:
        accept_cmd = _tool("vmax_accept.py") + [
            "--smsd", a.smsd, "--secret", a.secret, "--smsd-selftest",
            "--out", os.path.join(out_dir, "accept.json")] + common
        if a.skip_preflight:
            accept_cmd += ["--skip-preflight"]

    if a.dry_run:
        print("[ready] dry run -- nothing is probed, nothing is spent", flush=True)
        if accept_cmd:
            print("  accept: " + mask(" ".join(accept_cmd)), flush=True)
        print("  probe : %s --probe %s" % (os.path.join(HERE, "vmax_sms.py"), mask(sms)), flush=True)
        print("  day   : " + mask(" ".join(day_cmd)), flush=True)
        print("  card  : %s (first row only)" % card_csv, flush=True)
        return 0

    os.makedirs(logs, exist_ok=True)
    with open(card_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone", "username", "password", "proxy"])
        w.writerow([card["phone"], card.get("username", ""), card.get("password", ""),
                    card.get("proxy", "")])

    steps: list[dict] = []
    verdict: dict = {
        "stamp": time.strftime("%Y-%m-%d %H:%M:%S"), "phone": card["phone"], "base": base,
        "verdict": "unknown", "headline": "", "exit_code": None,
        "checks": [], "soft": [], "cost": {}, "steps": steps,
        "contract": os.path.abspath(a.contract) if a.contract else None,
        "artifacts": {"card": card_csv, "out_dir": out_dir},
    }

    def finish(code: int, name: str, headline: str) -> int:
        verdict["verdict"] = name
        verdict["headline"] = headline
        verdict["exit_code"] = code
        jpath, mpath = write_report(out_dir, verdict)
        print("", flush=True)
        print("[ready] verdict=%s exit=%d" % (name, code), flush=True)
        if verdict["checks"]:
            for c in verdict["checks"]:
                print("    %s %-16s %s" % (DONE if c["ok"] else FAIL, c["name"], c["detail"]),
                      flush=True)
        print("[ready] report: %s" % mpath, flush=True)
        print("[ready] machine: %s" % jpath, flush=True)
        return code

    # 0) offsets: only rows appended by *this* run are evidence.  Without this,
    #    re-running into the same --out reads last run's 303/token and prints
    #    READY for free (measured in R139: 0 SMS, 0 tokens, verdict READY).
    results_path = os.path.join(out_dir, "results.jsonl")
    logins_path = os.path.join(out_dir, "logins.jsonl")
    before = {"results": len(read_jsonl(results_path)),
              "logins": len(read_jsonl(logins_path))}
    verdict["run"] = {"results_before": before["results"], "logins_before": before["logins"]}
    verdict["artifacts"].update({"results": results_path, "logins": logins_path})
    if (before["results"] or before["logins"]) and not a.resume:
        print("[ready] this --out already holds %d result row(s) and %d login row(s) from an "
              "earlier run" % (before["results"], before["logins"]), file=sys.stderr)
        print("[ready] a day-one acceptance is one clean spend: use a fresh directory "
              "(runs\\ready-<date>) or pass --resume to judge only this run",
              file=sys.stderr)
        return finish(2, "dirty_out",
                      "这个 --out 里已经有上一轮的行（results=%d / logins=%d）：换一个新目录，"
                      "或 --resume 只认本轮新增" % (before["results"], before["logins"]))

    # 1) the receiver must answer before anything single-use is spent
    if not a.no_sms_check:
        chk = channel_check(sms, a.sms_dir)
        steps.append({"step": "probe", "ok": chk["ok"], "detail": chk["detail"]})
        print("[ready] sms channel: %s -- %s"
              % ("OK" if chk["ok"] else "DOWN", chk["detail"]), flush=True)
        if not chk["ok"] and not a.force:
            return finish(9, "sms_channel_down",
                          "收码通道没起：0 token / 0 短信 / 0 行结果（先修通道，或 --force）")
        if not chk["ok"]:
            print("[ready] --force: continuing with a dead SMS channel", file=sys.stderr)

    # 2) platform + inbox + modem state, and a synthetic SMS (no real card touched)
    if accept_cmd:
        rc, secs = run_live(accept_cmd, os.path.join(logs, "accept.log"))
        acc_path = os.path.join(out_dir, "accept.json")
        acc = {}
        if os.path.exists(acc_path):
            try:
                acc = json.load(open(acc_path, encoding="utf-8"))
            except ValueError:
                acc = {}
        steps.append({"step": "accept", "exit": rc, "seconds": secs,
                      "log": os.path.join(logs, "accept.log"),
                      "ready": acc.get("ready"), "hard_failures": acc.get("hard_failures")})
        if rc != 0:
            verdict["checks"] = [
                {"name": "accept_%s" % c.get("check"), "ok": bool(c.get("ok")),
                 "detail": str(c.get("detail"))[:120]}
                for c in (acc.get("checks") or []) if c.get("hard") and not c.get("ok")]
            return finish(1, "not_ready",
                          "验收闸门自己没过（平台/收件箱/模块）：0 token / 0 短信")

    # 3) the one real card: farm -> register -> login verify -> report
    rc, secs = run_live(day_cmd, os.path.join(logs, "day.log"))
    steps.append({"step": "day", "exit": rc, "seconds": secs,
                  "log": os.path.join(logs, "day.log")})
    all_results = read_jsonl(results_path)
    all_logins = read_jsonl(logins_path)
    results = all_results[before["results"]:]
    logins = all_logins[before["logins"]:]
    verdict["run"].update({"results_total": len(all_results), "results_new": len(results),
                           "logins_total": len(all_logins), "logins_new": len(logins)})
    man = {}
    man_path = os.path.join(out_dir, "manifest.json")
    if os.path.exists(man_path):
        try:
            man = json.load(open(man_path, encoding="utf-8"))
        except ValueError:
            man = {}
    verdict["artifacts"].update({"results": results_path, "logins": logins_path,
                                 "manifest": man_path})

    if rc == 9:
        return finish(9, "sms_channel_down",
                      "收码通道在批量阶段判定为不可用：农场没起、0 token / 0 短信")
    if rc == 3 or (results and results[0].get("register_error_kind") == "rate_limited"):
        return finish(3, "banned",
                      "`/register` 写桶还在 429 封禁里：等 ≥200 s 再跑（期间不要探活）")
    if not results and all_results:
        print("[ready] this run appended 0 result row(s); the %d row(s) on disk are from an "
              "earlier run and cannot stand as today's evidence" % len(all_results),
              file=sys.stderr)
        return finish(1, "stale_artifacts",
                      "本轮 0 行（day exit %d）：盘上 %d 行是上一轮的，不能当本轮证据"
                      % (rc, len(all_results)))
    if not results:
        return finish(1, "not_ready",
                      "没有结果行：day exit %d，见 logs/day.log" % rc)

    j = judge(results, logins)
    verdict.update(checks=j["checks"], provenance=j["provenance"],
                   soft=j["soft"], cost=j["cost"])
    verdict["next"] = mask(
        "python vmax_day.py --input %s --sms \"%s\" --sms-dir %s "
        "--out runs\\<date>\\results.jsonl --farm-workers 3 --on-taken reset --yes"
        % (a.input, sms, a.sms_dir))
    if not j["ready"]:
        failed = [c["name"] for c in j["checks"] + j["provenance"] if not c["ok"]]
        return finish(1, "not_ready", "判据未过：%s" % ", ".join(failed))
    return finish(0, "ready",
                  "八条判据全过：303 + code/state + access_token + uid + 复验 ok"
                  "（且这行是本轮真短信换来的）")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[ready] interrupted", file=sys.stderr)
        raise SystemExit(130)
