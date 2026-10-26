# -*- coding: utf-8 -*-
"""Acceptance gate: prove every hop except the SIM before spending a single SMS.

    python vmax_accept.py --smsd http://127.0.0.1:8787 --secret <TEST_SECRET>
    python vmax_accept.py --smsd ... --smsd-selftest      # phone -> PC path
    python vmax_accept.py --smsd ... --rehearse           # live captcha + /register
    python vmax_accept.py --smsd ... --phone 13... --sms "http:...&since={since}" --live --yes
    python vmax_accept.py --report accept.json --history "runs/accept-*.json"   # offline tables

What it checks
--------------
1. Auth platform: health / agreements / captcha scene / sms-config / 429 pacer state.
2. SMS inbox (`vmax_smsd`): health, **modem state when the daemon reports `modems[]`**,
   secret enforcement, /code semantics, optional
   synthetic ingest that proves the forwarder path end to end.
3. `--rehearse`: one real captcha solve + `/register` driven to the SMS gate.
4. `--live`: exactly one real account (needs `--yes`); prints status + uid.

Exit codes: 0 = ACCEPT_READY, 1 = not ready, 2 = refused (missing flag / bad args).
Evidence: writes `accept.json` with every check and the exact commands used.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vmax_formcost as formcost  # noqa: E402
from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()


from vmax_go import preflight  # noqa: E402

SELFTEST_PHONE = "13000000000"
SELFTEST_CODE = "123456"


def _add(rows, name, ok, detail, hard=True):
    rows.append({"check": name, "ok": bool(ok), "detail": detail, "hard": hard})
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}", flush=True)

STATUS_ORDER = ("ok", "skip", "warn", "fail")


def check_status(row: dict) -> str:
    """Map one check row to ok / skip / warn / fail (skip/warn = soft checks)."""
    ok = bool(row.get("ok"))
    hard = bool(row.get("hard", True))
    if ok:
        return "ok" if hard else "skip"
    return "fail" if hard else "warn"

def form_cost(rec: dict | None) -> dict:
    """(path, tokens bought for the form, device trust) from one register record."""
    rec = rec or {}
    kind = formcost.path(rec)          # "none" when the row never said
    dev = rec.get("device")
    dev = dev if isinstance(dev, dict) else {}
    extra = formcost.extra_tokens(kind)  # None = a label we do not know
    return {"path": kind, "extra_tokens": extra,
            "device_ok": dev.get("ok") if "ok" in dev else None}


def form_cost_ok(policy, cost: dict) -> bool:
    """Does the spend match the promise?  `fresh` pays by design; the rest ride free."""
    want = 1 if (policy or "auto") == "fresh" else 0
    return cost.get("extra_tokens") == want


def _fmt_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def render_report(report: dict, source: str = "", now: float | None = None,
                  detail_width: int = 110) -> list[str]:
    """Turn accept.json into a table a human can read at a glance."""
    now = time.time() if now is None else now
    rows = report.get("checks") or []
    out: list[str] = []
    out.append("ACCEPT REPORT" + (("  " + source) if source else ""))
    ts = report.get("ts") or 0
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "?"
    out.append(f"  when   {when}  (age {_fmt_age(now - ts) if ts else '?'})")
    out.append(f"  base   {report.get('base') or '?'}")
    ready = bool(report.get("ready"))
    out.append(f"  ready  {ready}" + ("" if ready else "   <-- NOT READY"))
    hard = report.get("hard_failures") or []
    out.append("  hard   " + (", ".join(str(h) for h in hard) if hard else "(none)"))
    if ts and now - ts > 3600:
        out.append("  note   older than 1h: re-run the gate before trusting this file")
    if not rows:
        out.append("  (no checks recorded)")
        return out
    counts = {k: 0 for k in STATUS_ORDER}
    body = []
    for i, r in enumerate(rows, 1):
        st = check_status(r)
        counts[st] += 1
        detail = " ".join(str(r.get("detail") or "").split())
        if len(detail) > detail_width:
            detail = detail[: detail_width - 3] + "..."
        body.append((i, st, str(r.get("check") or "?"), detail))
    w = max(len(b[2]) for b in body)
    out.append("  " + "-" * (7 + w + 2 + 4 + 2 + 20))
    out.append(f"  {'#':>3}  {'st':<4}  {'check':<{w}}  detail")
    for i, st, name, detail in body:
        out.append(f"  {i:>3}  {st:<4}  {name:<{w}}  {detail}")
    out.append("")
    tail = "  ".join(f"{k}={counts[k]}" for k in STATUS_ORDER)
    clean = counts["fail"] == 0 and counts["warn"] == 0
    out.append("  summary  " + tail + ("   ALL GOOD" if clean else ""))
    return out


def _report_stats(report: dict) -> tuple[dict, set]:
    """(counts by status, set of failing check names) for one accept report."""
    counts = {k: 0 for k in STATUS_ORDER}
    fails = set()
    for r in report.get("checks") or []:
        st = check_status(r)
        counts[st] += 1
        if st == "fail":
            fails.add(str(r.get("check") or "?"))
    return counts, fails


def collect_history(pattern: str, limit: int = 20) -> tuple:
    """Load every accept json matching a glob, oldest run first.

    Returns (entries, skipped). Each entry is {"path": str, "report": dict}.
    A half-written accept.json from an interrupted gate must not hide the rest,
    so unparsable / non-accept files are skipped and reported instead of fatal.
    """
    entries = []
    skipped = []
    for p in sorted(glob.glob(pattern)):
        try:
            with open(p, encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{p}: {type(e).__name__}")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
            skipped.append(f"{p}: not an accept report")
            continue
        entries.append({"path": p, "report": data})
    entries.sort(key=lambda e: (int(e["report"].get("ts") or 0), e["path"]))
    if limit and len(entries) > limit:
        entries = entries[-limit:]
    return entries, skipped


def render_history(entries: list, now: float | None = None,
                   skipped: list | None = None) -> list:
    """One line per past gate run, plus what changed since the run before it."""
    now = time.time() if now is None else now
    out = [f"ACCEPT HISTORY  {len(entries)} run(s)"]
    if skipped:
        head = "; ".join(skipped[:3])
        out.append(f"  skipped  {len(skipped)} file(s): {head}"
                   + (" ..." if len(skipped) > 3 else ""))
    if not entries:
        out.append("  (no accept reports matched)")
        return out

    rows = []
    prev_fails = None
    for e in entries:
        rep = e["report"]
        ts = int(rep.get("ts") or 0)
        when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"
        counts, fails = _report_stats(rep)
        delta = "."
        if prev_fails is not None:
            parts = []
            new = fails - prev_fails
            fixed = prev_fails - fails
            if new:
                parts.append(f"+{len(new)}")
            if fixed:
                parts.append(f"-{len(fixed)}")
            delta = " ".join(parts) or "="
        rows.append((when, os.path.basename(e["path"]), bool(rep.get("ready")),
                     counts, delta, fails))
        prev_fails = fails

    wsrc = min(max([len(r[1]) for r in rows] + [6]), 26)
    out.append("  " + "-" * (11 + 2 + wsrc + 2 + 5 + 2 + 4 + 2 + 4 + 2 + 5))
    out.append(f"  {'when':<11}  {'source':<{wsrc}}  {'ready':<5}  {'fail':>4}  "
               f"{'warn':>4}  {'chg':<5}")
    for when, src, ready, counts, delta, _f in rows:
        name = src if len(src) <= wsrc else src[: wsrc - 1] + "~"
        out.append(f"  {when:<11}  {name:<{wsrc}}  {'yes' if ready else 'NO':<5}  "
                   f"{counts['fail']:>4}  {counts['warn']:>4}  {delta:<5}")

    ready_n = sum(1 for r in rows if r[2])
    out.append("")
    out.append(f"  ready    {ready_n}/{len(rows)} runs")
    last_ts = int(entries[-1]["report"].get("ts") or 0)
    if last_ts:
        out.append("  latest   " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_ts))
                   + f"  (age {_fmt_age(now - last_ts)})")

    for i in range(len(rows) - 1, 0, -1):
        if rows[i][2] != rows[i - 1][2]:
            out.append(f"  changed  {rows[i][0]}  verdict -> "
                       f"{'ready' if rows[i][2] else 'NOT ready'}"
                       f"  ({os.path.basename(entries[i]['path'])})")
            break
    else:
        out.append("  changed  (verdict stable across the runs shown)")

    moves = []
    pf = None
    for i, e in enumerate(entries):
        _c, fails = _report_stats(e["report"])
        if pf is not None and fails != pf:
            when = rows[i][0]
            for name in sorted(pf - fails):
                moves.append(f"  {when}  {name}: fail -> ok")
            for name in sorted(fails - pf):
                moves.append(f"  {when}  {name}: ok -> fail")
        pf = fails
    if moves:
        out.append("")
        out.append("  check changes")
        out.extend(moves[-10:])
        if len(moves) > 10:
            out.append(f"  ... {len(moves) - 10} older change(s) not shown")
    return out


def print_history(pattern: str, limit: int = 20) -> int:
    """Print the history table. 0 = at least one run found, 2 = nothing usable."""
    entries, skipped = collect_history(pattern, limit)
    for line in render_history(entries, skipped=skipped):
        print(line, flush=True)
    return 0 if entries else 2


def report_file(path: str) -> int:
    """Print an existing accept.json. Returns 0 ready / 1 not ready / 2 unusable."""
    if not os.path.exists(path):
        print(f"[accept] no report at {os.path.abspath(path)} -- run the gate first", flush=True)
        return 2
    try:
        # utf-8-sig: tolerate a BOM if the file was re-saved by Notepad/PowerShell
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[accept] cannot parse {path}: {type(e).__name__}: {e}", flush=True)
        return 2
    if not isinstance(data, dict):
        print(f"[accept] {path} is not an accept.json object", flush=True)
        return 2
    for line in render_report(data, source=os.path.basename(path)):
        print(line, flush=True)
    return 0 if data.get("ready") else 1


def smsd_checks(url: str, secret: str, selftest: bool) -> list[dict]:
    import requests

    rows: list[dict] = []
    url = url.rstrip("/")
    try:
        r = requests.get(url + "/health", timeout=10)
        body = r.json() if r.content else {}
        _add(rows, "smsd_health", r.status_code == 200 and body.get("ok") is True,
             f"{r.status_code} ingested={body.get('ingested')} stored={body.get('stored')}")
        # A live daemon is not a live receiver: `vmax_smsd --modem COM3` with the
        # module unplugged (or the SIM PIN-locked) still answers ok=true.  R113
        # added modems[] for exactly this, so ACCEPT_READY has to mean "something
        # is actually listening on a SIM slot", not "the Python process is up".
        modems = body.get("modems") if isinstance(body, dict) else None
        if isinstance(modems, list) and modems:
            up = [m for m in modems if isinstance(m, dict) and m.get("connected")]
            detail = ", ".join(
                "%s:%s" % (m.get("name"), "up" if m.get("connected") else
                           "down (%s)" % (m.get("last_error") or "no SIM / no answer"))
                for m in modems if isinstance(m, dict))
            _add(rows, "smsd_modem", bool(up), detail)
        else:
            _add(rows, "smsd_modem", True,
                 "no --modem configured (HTTP forwarder / file ingest mode)", hard=False)
    except Exception as e:  # noqa: BLE001
        _add(rows, "smsd_health", False, f"{type(e).__name__}: {e}")
        return rows

    q = ("?secret=" + secret) if secret else ""
    try:
        r = requests.get(url + "/latest" + q, timeout=10)
        _add(rows, "smsd_latest_auth", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        _add(rows, "smsd_latest_auth", False, f"{type(e).__name__}: {e}")

    if secret:
        try:
            r = requests.get(url + "/latest", timeout=10)
            _add(rows, "smsd_secret_enforced", r.status_code == 401, f"{r.status_code}")
        except Exception as e:  # noqa: BLE001
            _add(rows, "smsd_secret_enforced", False, f"{type(e).__name__}: {e}")

    try:
        r = requests.get(url + "/code?phone=13600136000" + (("&secret=" + secret) if secret else "")
                         + "&timeout=1", timeout=15)
        body = r.json() if r.content else {}
        _add(rows, "smsd_code_semantics", r.status_code == 404 and body.get("error") == "timeout",
             f"{r.status_code} {body.get('error')}")
    except Exception as e:  # noqa: BLE001
        _add(rows, "smsd_code_semantics", False, f"{type(e).__name__}: {e}")

    if selftest:
        try:
            headers = {"X-Sms-Secret": secret} if secret else {}
            payload = {"phone": SELFTEST_PHONE,
                       "text": "【VobileMax】您的验证码为 " + SELFTEST_CODE + "，5 分钟内有效。"}
            r = requests.post(url + "/ingest", json=payload, headers=headers, timeout=10)
            got = (r.json() or {}).get("code") if r.content else None
            _add(rows, "smsd_ingest", r.status_code == 200 and got == SELFTEST_CODE,
                 f"{r.status_code} parsed={got}")
            r = requests.get(url + "/code?phone=" + SELFTEST_PHONE
                             + (("&secret=" + secret) if secret else "") + "&timeout=3", timeout=10)
            got2 = (r.json() or {}).get("code") if r.content else None
            _add(rows, "smsd_readback", got2 == SELFTEST_CODE, f"{r.status_code} code={got2}")
        except Exception as e:  # noqa: BLE001
            _add(rows, "smsd_ingest", False, f"{type(e).__name__}: {e}")
    else:
        _add(rows, "smsd_selftest", True,
             "skipped (--smsd-selftest proves phone->PC; note it writes a fake 13000000000 SMS)",
             hard=False)
    return rows


def _run(cmd: list[str], echo_to: str | None = None) -> tuple[int, str]:
    print("[run] " + " ".join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
    if echo_to:
        os.makedirs(os.path.dirname(os.path.abspath(echo_to)) or ".", exist_ok=True)
        with open(echo_to, "w", encoding="utf-8") as f:
            f.write(out)
    tail = "\n".join(out.strip().splitlines()[-6:])
    print(tail, flush=True)
    return p.returncode, out


def run_rehearse(a) -> tuple[bool, str]:
    cmd = [sys.executable, "vmax_go.py", "--rehearse"]
    if a.base:
        cmd += ["--base", a.base]
    if a.redirect:
        cmd += ["--redirect", a.redirect]
    if a.platform:
        cmd += ["--platform", a.platform]
    if a.proxy:
        cmd += ["--proxy", a.proxy]
    rc, out = _run(cmd)
    return (rc == 0 and "READY=True" in out), out


def run_live(a) -> tuple[bool, str, dict | None]:
    out_path = os.path.abspath("accept_live.jsonl")
    if os.path.exists(out_path):
        os.replace(out_path, out_path + ".prev")
    cmd = [sys.executable, "vmax_register.py", "--phone", a.phone, "--sms", a.sms,
           "--captcha", a.captcha, "--queue-dir", a.queue_dir,
           "--sms-resends", str(a.sms_resends), "--form-captcha", a.form_captcha,
           "--out", out_path]
    if a.captcha_param:
        cmd += ["--captcha-param", a.captcha_param]
    if a.max_age:
        os.environ["VMAX_TOKEN_MAX_AGE"] = str(a.max_age)
    if a.base:
        cmd += ["--base", a.base]
    if a.redirect:
        cmd += ["--redirect", a.redirect]
    if a.platform:
        cmd += ["--platform", a.platform]
    if a.proxy:
        cmd += ["--proxy", a.proxy]
    rc, out = _run(cmd)
    rec = None
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                rec = json.loads(f.read().strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            rec = None
    ok = rc == 0 and bool(rec) and rec.get("status") == "ok" and bool(rec.get("uid"))
    return ok, out, rec


def main() -> int:
    ap = argparse.ArgumentParser(description="one-command acceptance gate for the vmax pipeline")
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--smsd", default=None, help="inbox base URL, e.g. http://127.0.0.1:8787")
    ap.add_argument("--secret", default=os.environ.get("SMSD_SECRET", ""))
    ap.add_argument("--smsd-selftest", action="store_true",
                    help="POST a synthetic SMS to /ingest to prove the forwarder path")
    ap.add_argument("--rehearse", action="store_true", help="live captcha + /register to the SMS gate")
    ap.add_argument("--live", action="store_true", help="register ONE real account (needs --yes)")
    ap.add_argument("--yes", action="store_true", help="confirm the real account creation")
    ap.add_argument("--phone", default=None)
    ap.add_argument("--sms", default="prompt")
    ap.add_argument("--captcha", default="queue", choices=["auto", "manual", "assist", "param", "queue"])
    ap.add_argument("--captcha-param", default="", help="pre-solved token for --captcha param")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip the platform checks (offline mock / already verified)")
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--sms-resends", type=int, default=1)
    ap.add_argument("--form-captcha", default="auto", choices=["auto", "reuse", "fresh", "device"])
    ap.add_argument("--max-age", type=float, default=None, help="VMAX_TOKEN_MAX_AGE override")
    ap.add_argument("--out", default="accept.json")

    ap.add_argument("--report", nargs="?", const="", default=None, metavar="PATH",
                    help="print a human table for an accept.json; runs no checks")
    ap.add_argument("--history", nargs="?", const="accept*.json", default=None, metavar="GLOB",
                    help="with --report: also print past runs (default accept*.json)")
    ap.add_argument("--history-only", nargs="?", const="accept*.json", default=None, metavar="GLOB",
                    help="print only the history table; runs no checks")
    ap.add_argument("--history-limit", type=int, default=20,
                    help="keep the newest N runs in the history table (0 = all)")
    a = ap.parse_args()

    rc = None
    if a.report is not None:
        rc = report_file(a.report or a.out)
    if a.history is not None or a.history_only is not None:
        if rc is not None:
            print(flush=True)
        pattern = a.history_only if a.history_only is not None else a.history
        hrc = print_history(pattern, a.history_limit)
        return rc if rc is not None else hrc
    if rc is not None:
        return rc

    if a.live and not a.yes:
        print("[accept] --live creates a real account: re-run with --yes when you mean it", flush=True)
        return 2
    if a.live and not a.phone:
        print("[accept] --live needs --phone", flush=True)
        return 2

    rows: list[dict] = []
    if a.skip_preflight:
        _add(rows, "preflight", True, "skipped (--skip-preflight)", hard=False)
    else:
        rows += preflight(a.base, a.redirect, a.proxy, a.platform, a.queue_dir,
                          "queue" if a.captcha == "queue" else "auto")
    if a.smsd:
        rows += smsd_checks(a.smsd, a.secret, a.smsd_selftest)
    else:
        _add(rows, "smsd", False, "not checked (pass --smsd http://127.0.0.1:8787)", hard=False)

    rehearse_ok = None
    if a.rehearse:
        rehearse_ok, _ = run_rehearse(a)
        _add(rows, "rehearse_ready", rehearse_ok, "READY=True" if rehearse_ok else "see output above")

    live_ok = None
    live_rec = None
    live_cost = None
    if a.live:
        live_ok, _, live_rec = run_live(a)
        live_cost = form_cost(live_rec)
        _add(rows, "live_account", live_ok,
             f"status={(live_rec or {}).get('status')} uid={(live_rec or {}).get('uid')}")
        # Soft: a paid form token is not a gate failure, but the gate must show it.
        _add(rows, "live_form_cost", form_cost_ok(a.form_captcha, live_cost),
             f"policy={a.form_captcha} path={live_cost['path']} "
             f"extra_tokens={live_cost['extra_tokens']} "
             f"device_ok={live_cost['device_ok']}", hard=False)

    hard_fail = [r["check"] for r in rows if r["hard"] and not r["ok"]]
    ready = not hard_fail
    report = {
        "ts": int(time.time()),
        "base": a.base or os.environ.get("VMAX_BASE") or "https://auth.vobilemax.cn",
        "ready": ready,
        "hard_failures": hard_fail,
        "rehearse": rehearse_ok,
        "live": ({"ok": live_ok, "status": (live_rec or {}).get("status"),
                  "uid": (live_rec or {}).get("uid"),
                  "form_captcha": (live_cost or {}).get("path"),
                  "form_extra_tokens": (live_cost or {}).get("extra_tokens"),
                  "device_ok": (live_cost or {}).get("device_ok")} if a.live else None),
        "checks": rows,
        "note": "no SMS is sent unless --smsd-selftest / --rehearse / --live is used",
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"[accept] wrote {os.path.abspath(a.out)}", flush=True)
    print(f"ACCEPT_READY={ready}" + ("" if ready else f"  hard_failures={hard_fail}"), flush=True)
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
