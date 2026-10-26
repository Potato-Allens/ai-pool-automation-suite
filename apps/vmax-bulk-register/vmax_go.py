# -*- coding: utf-8 -*-
"""One command from "a CSV of numbers" to "tokens on disk".

    python vmax_go.py --input accounts.csv --out results.jsonl \
        --captcha queue --queue-dir tokens \
        --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=<TEST_SECRET>"

What it adds over calling vmax_batch.py directly
-----------------------------------------------
1. **Preflight first** -- health / agreements / captcha / sms-config / pacer state.
   Refuses to start while the register-write bucket is still in a 429 ban
   (see docs/PROTOCOL.md S7.2) instead of burning SMS codes on doomed requests.
2. **Capacity-aware plan** -- prints how many numbers, how many distinct
   egress IPs, and the realistic wall-clock ETA at ~30 register writes/h per
   egress IP (R156: 5/600 s; a 7-write 3 s burst does pass on a fresh bucket,
   but the 429 behind it outlives 27 min of silence).  Concurrency only helps
   with more proxies -- one IP, one lane.
3. **Abort on 429** -- one rate-limited answer stops the run instead of
   retrying into a longer ban.
4. **Resume** -- same ``--out`` skips phones already ``status=ok``.
5. ``--dry-run`` -- boots the offline isomorphic mock (tests/mock_e2e.py).
6. ``--rehearse`` -- live dry run against production: solves one captcha, spends it on an
   invalid number to prove the token is good, then drives ``/register`` to the SMS gate.
   No SMS, no account.
7. **SMS gate** -- probes the receiver before the first single-use token is spent.
   A dead channel otherwise costs one token and one real SMS per number.

Exit codes: 0 ok, 1 some accounts failed, 2 preflight failed, 3 aborted on 429,
4 nothing to do, 5 --max-fail reached, 6 --max-minutes reached,
9 the SMS channel is down -- before the first row: nothing was spent;
  mid-run (--sms-recheck): the rows already written stay and a rerun resumes.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from vmax_console import utf8_stdout  # noqa: E402
import vmax_formcost as formcost  # noqa: E402

utf8_stdout()


from vmax_batch import (dedupe_accounts, load_accounts, load_done,  # noqa: E402
                        load_skipped)
from vmax_client import VmaxAuth  # noqa: E402
from vmax_pacer import Pacer  # noqa: E402
from vmax_register import (RegisterError, classify_register_error,  # noqa: E402
                           register_one)
from vmax_reset import load_limited, probe_live, probe_schema, reset_one  # noqa: E402
from vmax_sms import channel_check  # noqa: E402
import vmax_journal as journal  # noqa: E402
import vmax_jsonl as jsonl  # noqa: E402

#: R156: the /register bucket is keyed on the egress IP and its *sustained*
#: budget is ~30 writes/h (5 per 600 s).  A 7-write 3 s burst passes on a fresh
#: bucket, so the old "5 per 60 s" (=300/h) oversold one IP by ~10x.
WRITES_PER_HOUR = 30.0
#: 1 write + the retry on the ~15% of numbers whose first code is wrong.
WRITES_PER_ACCOUNT = 1.18


def preflight(base: str | None, redirect: str | None, proxy: str | None,
              platform: str, queue_dir: str, captcha_mode: str,
              on_taken: str = "skip") -> list[dict]:
    cli = VmaxAuth(proxy=proxy, base=base, redirect_uri=redirect)
    out: list[dict] = []

    def add(name, ok, detail, hard=True):
        out.append({"check": name, "ok": bool(ok), "detail": detail, "hard": hard})
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    try:
        r = cli.s.get(f"{cli.base}/health/ready", timeout=15)
        add("health", r.status_code == 200, f"{r.status_code} {r.text[:50]}")
    except Exception as e:  # noqa: BLE001
        add("health", False, f"{type(e).__name__}: {e}")

    try:
        ag = cli.agreements()
        add("agreements", len(ag) >= 2, ", ".join(f"{a['id']}:{a['type']}" for a in ag))
    except Exception as e:  # noqa: BLE001
        add("agreements", False, f"{type(e).__name__}: {e}")

    try:
        cap = cli.captcha_config(platform)
        add("captcha", bool(cap.get("enabled")),
            f"scene={cap.get('scene_id')} prefix={cap.get('prefix')}")
    except Exception as e:  # noqa: BLE001
        add("captcha", False, f"{type(e).__name__}: {e}")

    try:
        cfg = cli.sms_config()
        add("sms_enabled", bool(cfg.get("enabled")), json.dumps(cfg, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        add("sms_enabled", False, f"{type(e).__name__}: {e}")

    try:
        snap = Pacer().snapshot("register@" + (proxy or "direct"))
        if snap["banned"]:
            add("pace", False,
                f"429 ban active: {snap['ban_remaining_s']:.0f}s left (strikes={snap['strikes']}); "
                f"sleep, never poll -- `python vmax_pacer.py status`")
        else:
            add("pace", True,
                f"{snap['window_used']}/{snap['capacity']} used in {snap['window_s']:.0f}s")
    except Exception as e:  # noqa: BLE001
        add("pace", True, f"pacer unavailable ({type(e).__name__})", hard=False)

    if captcha_mode == "queue":
        names = ([f for f in os.listdir(queue_dir)
                  if f.startswith("tok_") and f.endswith(".json")]
                 if os.path.isdir(queue_dir) else [])
        if not names:
            add("token_queue", False, f"0 tokens in {queue_dir} (farm must run alongside)", hard=False)
        else:
            import time as _t
            ages = []
            for n in names:
                try:
                    ages.append(_t.time() - os.path.getmtime(os.path.join(queue_dir, n)))
                except OSError:
                    pass
            oldest = max(ages) if ages else 0.0
            # Measured TTL is ~90-120 s (PROTOCOL S6.6).
            add("token_queue", oldest <= 120,
                f"{len(names)} tokens, oldest {oldest:.0f}s "
                f"({'fresh' if oldest <= 120 else 'STALE - they will be dropped'})",
                hard=False)

    # The recovery flow only matters with --on-taken reset, but its schema is
    # always worth one GET: a removed route is cheaper to learn now than after a
    # token and an SMS were spent on a number we cannot reset.
    schema_ok = False
    try:
        sch = probe_schema(cli)
        detail = ("send-code={} reset={} login={} fields={} paths={}".format(
            sch["has_send_code"], sch["has_reset"], sch["has_login"],
            ",".join(sch["request_fields"]) or "-", sch["paths"]))
        schema_ok = (sch["has_send_code"] and sch["has_reset"] and sch["has_login"]
                     and sch["captcha_free"])
        add("reset_schema", schema_ok, detail, hard=(on_taken == "reset"))
    except Exception as e:  # noqa: BLE001
        add("reset_schema", False, f"{type(e).__name__}: {e}", hard=(on_taken == "reset"))

    # Without a schema match there is no point touching the endpoint we think is
    # there: the failure above already says the flow moved.
    if on_taken == "reset" and schema_ok:
        try:
            pr = probe_live(cli)
            add("reset_probe", pr["ok"], f"{pr['status']} {pr['message']}", hard=True)
        except Exception as e:  # noqa: BLE001
            add("reset_probe", False, f"{type(e).__name__}: {e}", hard=True)
    return out


def plan(todo: list[dict], default_proxy: str | None) -> dict:
    proxies = {(a.get("proxy") or default_proxy) or "direct" for a in todo}
    lanes = max(1, len(proxies))
    writes = len(todo) * WRITES_PER_ACCOUNT
    eta_s = writes / (WRITES_PER_HOUR * lanes) * 3600.0
    return {"phones": len(todo), "lanes": lanes,
            "proxies": sorted(proxies),
            "writes": round(writes, 1),
            "write_budget_per_lane": WRITES_PER_HOUR,
            "accounts_per_hour": round(WRITES_PER_HOUR * lanes / WRITES_PER_ACCOUNT, 1),
            "rate_per_lane": f"{WRITES_PER_HOUR:.0f} writes/h (R156)",
            "eta_s": int(eta_s),
            "eta_human": _human(eta_s)}


def _human(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s" if m else f"{s}s"


def run_one(acc: dict, args) -> dict:
    ns = SimpleNamespace(
        phone=acc["phone"], username=acc.get("username"), password=acc.get("password"),
        platform=args.platform, invite="",
        captcha=args.captcha, captcha_param=getattr(args, "captcha_param", ""),
        captcha_attempts=args.captcha_attempts,
        queue_dir=args.queue_dir, queue_wait=args.queue_wait,
        assist_dir=args.assist_dir, stdin=args.stdin, headed=args.headed,
        sms=args.sms, sms_dir=args.sms_dir, sms_timeout=args.sms_timeout,
        sms_resends=args.sms_resends, proxy=acc.get("proxy") or args.proxy,
        form_captcha=getattr(args, "form_captcha", "auto"),
        debug_dir=args.debug_dir, out=None,
        base=args.base, redirect=args.redirect, allow_virtual=args.allow_virtual,
        journal=getattr(args, "journal", None),
    )
    try:
        return register_one(ns)
    except RegisterError as e:
        # A classified failure still carries the partial row: keep the send-code
        # answer, the captcha timing and the HTTP status.  The bucket matters --
        # `phone_taken` must exit 7 (and be skipped on resume, never retried for
        # another SMS), `rate_limited` must exit 3 and stop the whole run.
        res = dict(e.result)
        res.update({"phone": acc["phone"], "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                    "register_error_kind": e.kind, "ts": int(time.time())})
        return res
    except Exception as e:  # noqa: BLE001 - one bad number must not kill the run
        return {"phone": acc["phone"], "status": "error",
                "error": f"{type(e).__name__}: {e}",
                # an unexpected exception can still be a 429 page: classify it
                # rather than reporting a plain failure (which would not abort)
                "register_error_kind": classify_register_error(str(e)),
                "ts": int(time.time())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="vobilemax: preflight + paced batch registration")
    ap.add_argument("--input", help="CSV/JSONL of phones (header: phone[,username,password,proxy])")
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--journal", default=None,
                    help="write-ahead log (default: <out>.journal). /register 303 is written "
                         "there before the row exists, so a resumed run rebuilds it with "
                         "0 token / 0 SMS instead of paying for a password reset")
    ap.add_argument("--no-journal", action="store_true",
                    help="neither read nor write the journal")
    ap.add_argument("--summary", default=None, help="default: <out>.summary.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-fail", type=int, default=0,
                    help="stop the run after N failures (0 = never); exit 5")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="stop the run after N minutes (0 = no limit); exit 6")
    ap.add_argument("--delay", type=float, default=0.0, help="extra sleep per account (pacer already paces)")
    ap.add_argument("--captcha", default="queue",
                    choices=["auto", "manual", "assist", "queue", "param"])
    ap.add_argument("--captcha-param", default="",
                    help="pre-solved token for --captcha param (e.g. from the farm)")
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=600)
    ap.add_argument("--captcha-attempts", type=int, default=6)
    ap.add_argument("--sms", default=None,
                    help="receiver template (default: the local relay; it is probed "
                         "before the first token -- see --no-sms-check)")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-timeout", type=int, default=300)
    ap.add_argument("--sms-recheck", type=int, default=5,
                    help="probe the receiver every N accounts (0 = off); a run can last "
                         "hours and a channel that dies mid-run costs a token per number")
    ap.add_argument("--sms-recheck-seconds", type=float, default=300.0,
                    help="also probe when this many seconds have passed (0 = off)")
    ap.add_argument("--no-sms-check", action="store_true",
                    help="skip the pre-token SMS channel probe (costs 0 tokens, 0 SMS)")
    ap.add_argument("--form-captcha", default="auto",
                    choices=["auto", "reuse", "fresh", "device"])
    ap.add_argument("--sms-resends", type=int, default=0)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--allow-virtual", action="store_true")
    ap.add_argument("--retry-limited", action="store_true",
                    help="with --on-taken reset, also retry numbers whose latest row "
                         "today is a reset 429 (each attempt costs a captcha token)")
    ap.add_argument("--on-taken", default="skip", choices=["skip", "reset"],
                    help="number the server says is already registered: skip it, or take "
                         "the account over with the password-reset flow (1 token + 1 SMS)")
    ap.add_argument("--debug-dir", default="captcha_debug")
    ap.add_argument("--assist-dir", default="assist")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="run the offline mock E2E instead")
    ap.add_argument("--rehearse", action="store_true",
                    help="live rehearsal without SMS/accounts (solves a captcha, probes the SMS gate)")
    ap.add_argument("--force", action="store_true", help="start even if a hard preflight check fails")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = ap.parse_args(argv)
    if args.no_journal:
        args.journal = None
    elif not args.journal:
        args.journal = journal.default_path(args.out)
    # `--sms` is None by default so "the operator named a channel" stays visible;
    # the value it falls back to is still the local relay from the RUNBOOK.
    sms_given = args.sms is not None
    if args.sms is None:
        args.sms = "http://127.0.0.1:8787/code?phone={phone}"
    args.sms_given = sms_given

    # Wall clock starts before preflight: the cap is a *run* budget, and
    # measuring from here makes an already-tiny cap deterministic instead of
    # racing the microseconds between the old `started = time.time()` and the
    # first loop iteration.
    started = time.time()

    if args.rehearse:
        script = os.path.join(HERE, "vmax_rehearse.py")
        print(f"[go] rehearsal: {script}", flush=True)
        return subprocess.call([sys.executable, script] +
                               (["--base", args.base] if args.base else []) +
                               (["--redirect", args.redirect] if args.redirect else []) +
                               (["--proxy", args.proxy] if args.proxy else []) +
                               ["--platform", args.platform, "--out", "rehearse.json"])

    if args.dry_run:
        script = os.path.join(HERE, "tests", "mock_e2e.py")
        env = dict(os.environ, VMAX_RESEND_WAIT="0", VMAX_NOPACE="1")
        print(f"[go] dry run: {script}", flush=True)
        rc = subprocess.call([sys.executable, script], env=env)
        print(f"[go] mock E2E exit {rc}", flush=True)
        return 0 if rc == 0 else 1

    print(f"[go] preflight against {(args.base or 'https://auth.vobilemax.cn')}", flush=True)
    checks = preflight(args.base, args.redirect, args.proxy, args.platform,
                       args.queue_dir, args.captcha,
                       getattr(args, "on_taken", "skip"))
    hard_fail = [c for c in checks if c["hard"] and not c["ok"]]
    if args.preflight_only:
        # Only when a channel was named: --preflight-only must stay usable
        # without one (it is the "is the site itself alive" switch).
        if sms_given and not args.no_sms_check:
            chk = channel_check(args.sms, args.sms_dir)
            print("[go] sms channel: %s -- %s"
                  % ("OK" if chk["ok"] else "DOWN", chk["detail"]), flush=True)
            if not chk["ok"] and not args.force:
                return 9
        return 0 if not hard_fail else 2
    if not args.input:
        print("[go] --input is required (or use --preflight-only / --dry-run)", file=sys.stderr)
        return 2
    if hard_fail and not args.force:
        print(f"[go] {len(hard_fail)} hard check(s) failed -- fix them or pass --force", file=sys.stderr)
        return 2

    accounts, dups = dedupe_accounts(load_accounts(args.input))
    if dups:
        print(f"[go] collapsed {dups} duplicate phone row(s): one SMS per number applies", flush=True)
    done = load_done(args.out)
    recovered_rows, unresolved = [], []
    if getattr(args, "journal", None):
        recovered_rows, unresolved = journal.recover(args.journal, args.out)
        if recovered_rows:
            # Append first, print second: if we die here the rows are already on disk.
            # One locked write: two processes must never merge two rows into one.
            jsonl.append_lines(args.out, recovered_rows)
            done |= {r["phone"] for r in recovered_rows}
            print(f"[go] journal: recovered {len(recovered_rows)} account(s) that were "
                  f"registered but never written -- 0 token, 0 SMS ({args.journal})", flush=True)
        if unresolved:
            print(f"[go] journal: {len(unresolved)} number(s) reached /register 303 but the "
                  f"token exchange never finished (no uid). They will answer phone_taken; "
                  f"--on-taken reset (1 SMS) takes them back.", flush=True)
    # With --on-taken reset an already-registered number is not dead: only the ones
    # that answered 429 today stay blocked (0 token); tomorrow they are eligible.
    blocked = load_skipped(args.out, rescue_reset=getattr(args, "on_taken", "skip") == "reset",
                           retry_limited=getattr(args, "retry_limited", False))
    capped_skipped = set() if getattr(args, "retry_limited", False) \
        else load_limited(args.out) & {a["phone"] for a in accounts}
    todo = [a for a in accounts if a["phone"] not in done and a["phone"] not in blocked]
    if args.limit:
        todo = todo[: args.limit]
    if blocked:
        print(f"[go] {len(blocked)} phone(s) already registered elsewhere -> skipped "
              f"(no token, no SMS)", flush=True)
    if capped_skipped:
        print(f"[go] {len(capped_skipped)} phone(s) hit today's reset cap -> skipped for the "
              f"rest of the day (0 token; retry tomorrow or --retry-limited)", flush=True)
    if not todo:
        if recovered_rows:
            print(f"[go] nothing left to register -- {len(recovered_rows)} row(s) came back "
                  f"from the journal, so this run did do the work", flush=True)
        else:
            print("[go] nothing to do (every phone is status=ok, already registered, or "
                  "capped for today)", flush=True)
        # Still leave a summary: vmax_day reads it to name the verdict, and "we skipped
        # two numbers because today's quota is gone" is not the same as "pool empty".
        summary = {
            "ts": int(time.time()), "elapsed_s": round(time.time() - started, 1),
            "input": os.path.abspath(args.input), "out": os.path.abspath(args.out),
            "total_rows": len(accounts), "planned": 0, "ok": 0, "fail": 0,
            "taken": 0, "reused": 0, "reset_limited": False,
            "skipped_capped": len(capped_skipped), "nothing_to_do": True,
            "aborted_on_429": False, "stopped_on_max_fail": False,
            "stopped_on_max_minutes": False, "duplicates_collapsed": dups,
            "error_kinds": {}, "plan": {},
            "journal_recovered": len(recovered_rows),
            "form_captcha": getattr(args, "form_captcha", "auto") or "auto",
        **formcost.summarize({}),
        }
        sp = args.summary or (args.out + ".summary.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        print(f"[go] nothing spent -> {sp}", flush=True)
        # Recovery *is* work: the accounts exist and their rows are now on disk, so exit 0
        # rather than 4 ("nothing to do").  vmax_batch already behaves this way; this keeps
        # a resumed run from looking like a no-op to vmax_day (verdict) and to the operator.
        if capped_skipped:
            return 7
        return 0 if recovered_rows else 4

    # Probe the receiver *before* the first single-use captcha token.  vmax_day
    # did this since R114; a direct `vmax_go` run still burned one token and one
    # real SMS per number when the relay was down.
    if not args.no_sms_check:
        chk = channel_check(args.sms, args.sms_dir)
        print("[go] sms channel: %s -- %s"
              % ("OK" if chk["ok"] else "DOWN", chk["detail"]), flush=True)
        if not chk["ok"]:
            if not args.force:
                print("[go] refusing to spend tokens on a dead SMS channel "
                      "(fix it, or pass --no-sms-check / --force)", file=sys.stderr,
                      flush=True)
                return 9
            print("[go] --force: continuing with a dead SMS channel",
                  file=sys.stderr, flush=True)

    p = plan(todo, args.proxy)
    print(f"[go] {len(accounts)} rows, {len(done)} done, {len(todo)} to run", flush=True)
    print(f"[go] lanes={p['lanes']} proxies={p['proxies']} rate={p['rate_per_lane']}/lane "
          f"=> {p['accounts_per_hour']} accounts/h, ETA >={p['eta_human']}"
          f" (lower bound: excludes captcha/SMS wait)", flush=True)
    if len(p["proxies"]) == 1:
        print(f"[go] NOTE: single egress IP -- ~{p['accounts_per_hour']:.0f} accounts/h is the "
              f"ceiling; extra workers only hit 429 sooner (add proxies: one egress IP per worker)",
              flush=True)
    else:
        print(f"[go] NOTE: {p['lanes']} proxy URL(s) = {p['lanes']} lane(s) assumed, but a URL is "
              f"not a verified exit IP -- check each with `python vmax_doctor.py --proxy <url>` "
              f"first, two URLs behind one exit are still one 429 bucket", flush=True)

    if not args.yes and sys.stdin.isatty():
        try:
            if input(f"[go] start {len(todo)} registrations? [y/N] ").strip().lower() not in ("y", "yes"):
                print("[go] aborted by user")
                return 4
        except (EOFError, KeyboardInterrupt):
            return 4

    ok = fail = taken = reused = 0
    aborted = False
    reset_limited = False
    reset_hits = 0
    hit_max_fail = False
    hit_minutes = False
    reasons: dict[str, int] = {}
    # Form-token accounting (R149): the card bill is "1 token + 1 SMS" only when
    # the device path held for every row.  Counting the policy that each row
    # actually recorded is the only way to notice a silent 2x after the fact.
    form_counts: dict[str, int] = {}
    sms_state = {"probes": 0, "last": time.time(), "down": False, "detail": ""}
    for i, acc in enumerate(todo, 1):
        # check the wall-clock cap *before* spending another token/SMS, otherwise
        # a long account can overrun --max-minutes by its own duration (up to 5 min)
        if args.max_minutes and (time.time() - started) / 60.0 >= args.max_minutes:
            hit_minutes = True
            print(f"[go] --max-minutes {args.max_minutes:g} reached before row {i}/{len(todo)}; "
                  f"stopping (resume with the same --out)", flush=True)
            break
        res = run_one(acc, args)
        jsonl.append_lines(args.out, [res])
        _kind = res.get("form_captcha") or "none"
        form_counts[_kind] = form_counts.get(_kind, 0) + 1
        if res.get("status") == "ok":
            ok += 1
            print(f"[go] {i}/{len(todo)} {acc['phone']} ok uid={res.get('uid')}", flush=True)
        elif res.get("register_error_kind") == "phone_taken":
            # terminal, not a failure: skipping it on resume is the whole point, and
            # it must not consume the --max-fail budget meant for real errors.
            recovered = None
            if getattr(args, "on_taken", "skip") == "reset" and not reset_limited:
                recovered = reset_one(args, acc["phone"])
                jsonl.append_lines(args.out, [recovered])
                _kind = recovered.get("form_captcha") or "none"
                form_counts[_kind] = form_counts.get(_kind, 0) + 1
            elif getattr(args, "on_taken", "skip") == "reset":
                print(f"[go] {i}/{len(todo)} {acc['phone']} reset skipped "
                      f"(reset channel off: 2 daily-cap answers)", flush=True)
            if recovered is not None and recovered.get("status") == "ok":
                ok += 1
                reused += 1
                print(f"[go] {i}/{len(todo)} {acc['phone']} recovered by password reset "
                      f"uid={recovered.get('uid')} (via=reset)", flush=True)
            else:
                taken += 1
                reasons["phone_taken"] = reasons.get("phone_taken", 0) + 1
                note = (f"; reset failed: {recovered.get('reset_error_kind')}"
                        if recovered is not None else "")
                print(f"[go] {i}/{len(todo)} {acc['phone']} already registered -> skipped"
                      f"{note}", flush=True)
                if recovered is not None and recovered.get("reset_error_kind") == "daily_limit":
                    # The 429 arrives *after* the captcha gate, so every attempt costs a
                    # token.  Two different numbers answering 429 means the cap is not
                    # about one number: stop resetting, keep registering the rest.
                    reset_hits += 1
                    if reset_hits >= 2 and not reset_limited:
                        reset_limited = True
                        print("[go] 2 numbers hit the reset daily cap -> no more resets this "
                              "run (each further attempt would spend a captcha token to "
                              "learn the same 429). Taken rows stay for tomorrow; fresh "
                              "numbers keep going.", flush=True)
        else:
            fail += 1
            kind = res.get("register_error_kind") or "error"
            reasons[kind] = reasons.get(kind, 0) + 1
            print(f"[go] {i}/{len(todo)} {acc['phone']} {kind}: {str(res.get('error'))[:80]}", flush=True)
            if kind == "rate_limited" or "429" in str(res.get("error", "")):
                aborted = True
                print("[go] 429 -> stopping the run (do not poll; the ban renews). "
                      "Check `python vmax_pacer.py status`.", flush=True)
                break
        if args.delay:
            time.sleep(args.delay)
        # A batch can run for hours.  If the receiver dies at hour 2, every
        # remaining number burns one token and then waits --sms-timeout (5 min),
        # and the operator only learns it at the end.  Probe between accounts and
        # stop; finished rows are already in --out, so a rerun resumes.
        if not args.no_sms_check and i < len(todo):
            due = ((args.sms_recheck and i % args.sms_recheck == 0)
                   or (args.sms_recheck_seconds
                       and time.time() - sms_state["last"] >= args.sms_recheck_seconds))
            if due:
                chk = channel_check(args.sms, args.sms_dir)
                sms_state["probes"] += 1
                sms_state["last"] = time.time()
                sms_state["detail"] = chk["detail"]
                if not chk["ok"]:
                    sms_state["down"] = True
                    print(f"[go] sms channel went down after {i}/{len(todo)} "
                          f"({chk['detail']}) -> stopping before the next number "
                          f"(0 token, 0 SMS from here; finished rows are in {args.out})",
                          flush=True)
                    break
        elapsed_min = (time.time() - started) / 60.0
        if args.max_minutes and elapsed_min >= args.max_minutes:
            hit_minutes = True
            print(f"[go] --max-minutes {args.max_minutes:g} reached after {i}/{len(todo)}; "
                  f"stopping (resume with the same --out)", flush=True)
            break
        if args.max_fail and fail >= args.max_fail:
            hit_max_fail = True
            print(f"[go] --max-fail {args.max_fail} reached ({fail} failed); stopping. "
                  f"Look at the error kinds above before resuming.", flush=True)
            break

    summary = {
        "ts": int(time.time()), "elapsed_s": round(time.time() - started, 1),
        "input": os.path.abspath(args.input), "out": os.path.abspath(args.out),
        "total_rows": len(accounts), "planned": len(todo), "ok": ok, "fail": fail,
        "taken": taken, "reused": reused, "reset_limited": reset_limited,
        "skipped_capped": len(capped_skipped),
        "aborted_on_429": aborted, "stopped_on_max_fail": hit_max_fail,
        "stopped_on_max_minutes": hit_minutes, "duplicates_collapsed": dups,
        "stopped_on_sms_down": sms_state["down"],
        "sms_recheck": {"probes": sms_state["probes"], "detail": sms_state["detail"]},
        "error_kinds": reasons, "plan": p,
        "journal_recovered": len(recovered_rows),
        # Money audit: one shared price list (R152, vmax_formcost).  Exactly one
        # bucket per row (R150): device / reuse buy nothing new, the other four buy
        # one token, and anything unnamed is reported as form_unknown instead of
        # quietly joining the free ones.  Invariant on every new run:
        # form_device + form_reuse + form_extra_tokens + form_unknown == rows.
        "form_captcha": getattr(args, "form_captcha", "auto") or "auto",
        **formcost.summarize(form_counts),
    }
    sp = args.summary or (args.out + ".summary.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"[go] ok={ok} reused={reused} fail={fail} taken={taken} aborted={aborted} "
          f"reset_limited={reset_limited} in {summary['elapsed_s']}s -> {sp}", flush=True)
    if aborted:
        return 3
    if sms_state["down"]:
        return 9
    if hit_max_fail:
        return 5
    if hit_minutes:
        return 6
    if fail:
        return 1
    return 7 if (taken or capped_skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
