# -*- coding: utf-8 -*-
"""Batch registration driver for auth.vobilemax.cn.

Input (CSV with header, or JSONL, or plain text with one phone per line):
    phone,username,password,proxy
    13800000000,alice,Aa12345678,
    13900000000,,,http://user:pass@host:port

Every account gets its own VmaxAuth session (cookies), optionally its own
egress proxy, and - depending on --captcha - a fresh browser context.
Results are appended to results.jsonl so the run can be resumed.

Scaling:
    --workers N   parallel accounts (only for --captcha queue; Playwright is
                  not thread safe, so auto/assist/manual force workers=1)
    --shard i/n   take every n-th row starting at i (1-based) so several
                  machines can split one input file
    --delay S     sleep after each finished account (per worker)

Exit codes: 0 ok, 1 some failed, 2 bad input, 3 aborted on 429, 5 --max-fail,
6 --max-minutes, 7 the pool had taken numbers, 9 the SMS channel is down
(nothing spent before the first row; mid-run --sms-recheck: the rows already
written stay and a rerun resumes).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()


from vmax_register import (RegisterError, classify_register_error,  # noqa: E402
                           normalize_phone, register_one)
from vmax_reset import load_limited, reset_one  # noqa: E402
from vmax_sms import channel_check  # noqa: E402
import vmax_journal as journal  # noqa: E402
import vmax_jsonl as jsonl  # noqa: E402


def load_accounts(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        head = f.read(4096)
        f.seek(0)
        if path.lower().endswith(".jsonl") or head.lstrip().startswith("{"):
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        elif "," in head.splitlines()[0]:
            for r in csv.DictReader(f):
                rows.append({k.strip(): (v or "").strip() for k, v in r.items() if k})
        else:
            header_words = {"phone", "mobile", "手机号", "手机", "号码"}
            for i, line in enumerate(f):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if i == 0 and line.lower() in header_words:
                    continue
                rows.append({"phone": line})
    out = []
    for r in rows:
        phone = normalize_phone(str(r.get("phone") or r.get("mobile") or ""))
        if phone:
            out.append({"phone": phone,
                        "username": r.get("username") or None,
                        "password": r.get("password") or None,
                        "proxy": r.get("proxy") or None})
    return out


def dedupe_accounts(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapse repeated phones, keeping the first row.

    A duplicate is not just wasted time: the second attempt re-sends an SMS to
    the same number inside the 60s cooldown and burns a captcha token for
    nothing, so the runner refuses to queue the same phone twice.
    """
    seen: set[str] = set()
    out: list[dict] = []
    dups = 0
    for a in rows:
        ph = a.get("phone")
        if not ph:
            continue
        if ph in seen:
            dups += 1
            continue
        seen.add(ph)
        out.append(a)
    return out, dups


def load_done(path: str) -> set[str]:
    done: set[str] = set()
    if not os.path.exists(path):
        return done
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("status") == "ok" and r.get("phone"):
                done.add(r["phone"])
    return done


def load_skipped(path: str, rescue_reset: bool = False,
                 retry_limited: bool = False, day: str | None = None) -> set[str]:
    """Phones that must not be attempted again: the server says they are taken.

    ``phone_taken`` is terminal for this run (see
    ``vmax_register.classify_register_error``): retrying spends a captcha token for
    a guaranteed failure, so a resume skips it instead of grinding every run.

    With ``--on-taken reset`` the number is not dead -- the password reset can still
    reclaim it.  ``rescue_reset`` therefore blocks only the numbers *tried today*
    (a taken row or a reset 429 dated today): retrying them would spend another
    captcha token to relearn the same answer.  A number first found taken yesterday
    is eligible again today, and ``retry_limited`` drops even the same-day filter.
    """
    out: set[str] = set()
    if not path or not os.path.exists(path):
        return out
    latest: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("phone"):
                latest[str(r["phone"])] = r     # append order: last row wins
    if retry_limited:
        return set()
    if not rescue_reset:
        for r in latest.values():
            if r.get("register_error_kind") == "phone_taken" and r.get("status") != "ok":
                out.add(str(r["phone"]))
        return out
    # Rescue mode: "tried today" is what keeps a number blocked, wherever the day
    # boundary falls.  A number first found taken yesterday is eligible again today
    # even if yesterday's reset also answered 429 (the cap is daily).
    day = day or time.strftime("%Y-%m-%d")
    for ph, r in latest.items():
        if r.get("status") == "ok":
            continue
        try:
            when = time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts"))))
        except (TypeError, ValueError):
            when = day
        if when != day:
            continue
        if r.get("register_error_kind") == "phone_taken" \
                or r.get("reset_error_kind") == "daily_limit":
            out.add(ph)
    return out


def parse_shard(spec: str | None) -> tuple[int, int]:
    if not spec:
        return 0, 1
    try:
        idx, cnt = spec.split("/")
        idx, cnt = int(idx), int(cnt)
    except Exception:
        raise SystemExit(f"--shard wants i/n, got {spec!r}")
    if cnt < 1 or not (1 <= idx <= cnt):
        raise SystemExit(f"--shard out of range: {spec!r}")
    return idx - 1, cnt


def main():
    ap = argparse.ArgumentParser(description="vobilemax batch registration")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--journal", default=None,
                    help="write-ahead log (default: <out>.journal); recovery is free: "
                         "0 token, 0 SMS")
    ap.add_argument("--no-journal", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=5.0, help="seconds between accounts (per worker)")
    ap.add_argument("--workers", type=int, default=1, help="parallel accounts (queue mode only)")
    ap.add_argument("--shard", default=None, help="i/n, process every n-th row starting at i")
    ap.add_argument("--captcha", default="assist",
                    choices=["auto", "manual", "assist", "queue", "param"])
    ap.add_argument("--captcha-param", default="", help="pre-solved token for --captcha param")
    ap.add_argument("--max-fail", type=int, default=0,
                    help="stop after N failures (0 = never); exit 5")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="stop after N minutes (0 = no limit); exit 6")
    ap.add_argument("--queue-dir", default="tokens", help="token queue dir for --captcha queue")
    ap.add_argument("--queue-wait", type=float, default=0,
                    help="seconds to wait for a token when the queue is empty (0 = fail fast)")
    ap.add_argument("--captcha-attempts", type=int, default=6)
    ap.add_argument("--sms", default=None,
                    help="receiver template (default: file inbox in --sms-dir); it is "
                         "probed before the first token -- see --no-sms-check")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-timeout", type=int, default=300)
    ap.add_argument("--sms-recheck", type=int, default=5,
                    help="probe the receiver every N accounts (0 = off)")
    ap.add_argument("--sms-recheck-seconds", type=float, default=300.0,
                    help="also probe when this many seconds have passed (0 = off)")
    ap.add_argument("--no-sms-check", action="store_true",
                    help="skip the pre-token SMS channel probe (costs 0 tokens, 0 SMS)")
    ap.add_argument("--force", action="store_true",
                    help="start even if the SMS channel probe fails")
    ap.add_argument("--form-captcha", default="auto",
                    choices=["auto", "reuse", "fresh", "device"])
    ap.add_argument("--sms-resends", type=int, default=0,
                    help="re-send after the 60s cooldown when the code is rejected")
    ap.add_argument("--proxy", default=None, help="default proxy when the row has none")
    ap.add_argument("--debug-dir", default="captcha_debug")
    ap.add_argument("--assist-dir", default="assist")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--platform", default="dreammaker")
    ap.add_argument("--base", default=None, help="auth base URL (env VMAX_BASE)")
    ap.add_argument("--redirect", default=None, help="redirect_uri override (test env)")
    ap.add_argument("--allow-virtual", action="store_true")
    ap.add_argument("--retry-limited", action="store_true",
                    help="with --on-taken reset, also retry numbers whose latest row "
                         "today is a reset 429 (each attempt costs a captcha token)")
    ap.add_argument("--on-taken", default="skip", choices=["skip", "reset"],
                    help="number the server says is already registered: skip it, or take "
                         "the account over with the password-reset flow (1 token + 1 SMS)")
    args = ap.parse_args()
    if args.no_journal:
        args.journal = None
    elif not args.journal:
        args.journal = journal.default_path(args.out)
    sms_given = args.sms is not None
    if args.sms is None:
        args.sms = "file"
    args.sms_given = sms_given

    if args.workers > 1 and (args.captcha in ("auto", "assist", "manual") or args.sms == "prompt"):
        print("[batch] browser/stdin modes are single-threaded; forcing --workers 1", flush=True)
        args.workers = 1
    if args.workers > 1 and (args.max_fail or args.max_minutes):
        print("[batch] --max-fail/--max-minutes are checked on the sequential path; "
              "forcing --workers 1", flush=True)
        args.workers = 1

    accounts, dups = dedupe_accounts(load_accounts(args.input))
    if dups:
        print(f"[batch] collapsed {dups} duplicate phone row(s)", flush=True)
    shard_idx, shard_cnt = parse_shard(args.shard)
    if shard_cnt > 1:
        accounts = [a for i, a in enumerate(accounts) if i % shard_cnt == shard_idx]
    done = load_done(args.out)
    recovered_rows, unresolved = [], []
    if getattr(args, "journal", None):
        recovered_rows, unresolved = journal.recover(args.journal, args.out)
        if recovered_rows:
            jsonl.append_lines(args.out, recovered_rows)
            done |= {r["phone"] for r in recovered_rows}
            print(f"[batch] journal: recovered {len(recovered_rows)} account(s) that were "
                  f"registered but never written -- 0 token, 0 SMS ({args.journal})", flush=True)
        if unresolved:
            print(f"[batch] journal: {len(unresolved)} number(s) reached /register 303 but the "
                  f"token exchange never finished (no uid); --on-taken reset (1 SMS) takes them "
                  f"back", flush=True)
    blocked = load_skipped(args.out, rescue_reset=args.on_taken == "reset",
                           retry_limited=getattr(args, "retry_limited", False))
    capped = set() if getattr(args, "retry_limited", False) \
        else load_limited(args.out) & {a["phone"] for a in accounts}
    todo = [a for a in accounts if a["phone"] not in done and a["phone"] not in blocked]
    if args.limit:
        todo = todo[: args.limit]
    print(f"[batch] {len(accounts)} rows (shard {shard_idx+1}/{shard_cnt}), "
          f"{len(done)} already done, {len(todo)} to run, workers={args.workers}", flush=True)
    if blocked:
        print(f"[batch] {len(blocked)} phone(s) already registered elsewhere -> skipped "
              f"(no token, no SMS)", flush=True)
    if capped:
        print(f"[batch] {len(capped)} phone(s) hit today's reset cap -> skipped for the "
              f"rest of the day (0 token; retry tomorrow or --retry-limited)", flush=True)

    # Probe the receiver before the first row: a dead channel otherwise costs a
    # single-use captcha token and one real SMS per number (R114 did this for
    # vmax_day, R115 extends it to the direct drivers).
    if todo and not args.no_sms_check:
        chk = channel_check(args.sms, args.sms_dir)
        print("[batch] sms channel: %s -- %s"
              % ("OK" if chk["ok"] else "DOWN", chk["detail"]), flush=True)
        if not chk["ok"]:
            if not args.force:
                print("[batch] refusing to spend tokens on a dead SMS channel "
                      "(fix it, or pass --no-sms-check / --force)", file=sys.stderr,
                      flush=True)
                return 9
            print("[batch] --force: continuing with a dead SMS channel",
                  file=sys.stderr, flush=True)

    lock = threading.Lock()
    # "taken" is terminal, not a failure: a number someone already registered must
    # not trip --max-fail (a pool of recycled numbers would abort the whole run).
    counters = {"ok": 0, "fail": 0, "taken": 0, "reused": 0, "n": 0, "limited": 0}
    # A daily-capped reset answers 429 *after* the captcha gate, so every attempt
    # costs a token.  Two different numbers answering 429 means the cap is not
    # about one number: stop resetting (fresh registrations keep going).
    reset_state = {"hits": 0, "off": False, "skipped": 0}
    # One reset at a time, and the switch is re-read *after* winning the gate:
    # every lane can answer phone_taken in the same instant, and a capped channel
    # says 429 only after the captcha gate.  Without the gate N lanes each paid a
    # token to learn the same fact (R106 bounded this for a single worker only).
    reset_gate = threading.Lock()
    t_start = time.time()

    # Mid-run watchdog: a channel that dies at hour 2 otherwise costs one token
    # and one 5-minute wait per remaining number.  Finished rows are on disk, so
    # stopping is always resumable.
    hw = {"probes": 0, "last": time.time(), "down": False, "detail": "",
          "lock": threading.Lock()}

    def channel_alive(done: int) -> bool:
        if args.no_sms_check:
            return True
        if hw["down"]:
            return False
        if not ((args.sms_recheck and done and done % args.sms_recheck == 0)
                or (args.sms_recheck_seconds
                    and time.time() - hw["last"] >= args.sms_recheck_seconds)):
            return True
        with hw["lock"]:
            if hw["down"]:
                return False
            # Re-check under the lock: four lanes can finish at the same instant.
            if not ((args.sms_recheck and done and done % args.sms_recheck == 0)
                    or (args.sms_recheck_seconds
                        and time.time() - hw["last"] >= args.sms_recheck_seconds)):
                return True
            chk = channel_check(args.sms, args.sms_dir)
            hw["probes"] += 1
            hw["last"] = time.time()
            hw["detail"] = chk["detail"]
            if not chk["ok"]:
                hw["down"] = True
                print("[batch] sms channel went down after %d row(s) (%s) -> stopping "
                      "before the next number (0 token, 0 SMS from here)"
                      % (done, chk["detail"]), flush=True)
                return False
            return True

    def work(acc: dict) -> dict:
        ns = SimpleNamespace(
            phone=acc["phone"], username=acc["username"], password=acc["password"],
            platform=args.platform, invite="",
            captcha=args.captcha, captcha_param=getattr(args, "captcha_param", ""),
            captcha_attempts=args.captcha_attempts,
            queue_dir=args.queue_dir, queue_wait=args.queue_wait,
            assist_dir=args.assist_dir, stdin=args.stdin, headed=args.headed,
            sms=args.sms, sms_dir=args.sms_dir, sms_timeout=args.sms_timeout,
            sms_resends=getattr(args, "sms_resends", 0),
            form_captcha=getattr(args, "form_captcha", "auto"),
            proxy=acc["proxy"] or args.proxy, debug_dir=args.debug_dir, out=None,
            base=args.base, redirect=args.redirect, allow_virtual=args.allow_virtual,
            journal=getattr(args, "journal", None),
        )
        try:
            res = register_one(ns)
        except RegisterError as e:
            # keep the partial row and the bucket: this is what makes "phone_taken"
            # terminal (no --max-fail, no retry) and what the 429 abort reads
            res = dict(e.result)
            res.update({"phone": acc["phone"], "status": "error",
                        "error": f"{type(e).__name__}: {e}",
                        "register_error_kind": e.kind, "ts": int(time.time())})
        except Exception as e:  # noqa: BLE001 - one bad account must not kill the batch
            # keep the error bucket so the 429 abort below can see "rate_limited"
            # even though register_one signalled the failure with an exception
            res = {"phone": acc["phone"], "status": "error",
                   "error": f"{type(e).__name__}: {e}",
                   "register_error_kind": classify_register_error(str(e)),
                   "ts": int(time.time())}
        recovered = None
        capped = False
        if getattr(args, "on_taken", "skip") == "reset" and \
                res.get("register_error_kind") == "phone_taken":
            with reset_gate:
                if reset_state["off"]:
                    reset_state["skipped"] += 1
                    print(f"[batch] {acc['phone']} taken; reset channel already closed "
                          f"for this run -> skipped (0 token, 0 SMS), rerun tomorrow",
                          flush=True)
                else:
                    # Deliberately outside `lock`: this waits for a real SMS (minutes).
                    recovered = reset_one(ns, acc["phone"])
                    if (recovered or {}).get("reset_error_kind") == "daily_limit":
                        reset_state["hits"] += 1
                        capped = True
                        if reset_state["hits"] >= 2:
                            reset_state["off"] = True
                            print("[batch] 2 numbers hit the reset daily cap -> no more "
                                  "resets this run (each further attempt spends a captcha "
                                  "token to learn the same 429); fresh numbers keep "
                                  "registering", flush=True)
        with lock:
            batch = [res] + ([recovered] if recovered is not None else [])
            # NOTE: not `lock=lock` -- this thread already holds it; append_lines
            # takes its own global thread lock plus the cross-process file lock.
            jsonl.append_lines(args.out, batch)
            counters["n"] += 1
            if capped:
                counters["limited"] += 1
            if recovered is not None and recovered.get("status") == "ok":
                counters["ok"] += 1
                counters["reused"] += 1
            elif res.get("status") == "ok":
                counters["ok"] += 1
            elif res.get("register_error_kind") == "phone_taken":
                counters["taken"] += 1
            else:
                counters["fail"] += 1
            final = recovered if recovered is not None else res
            print(f"[batch] {counters['n']}/{len(todo)} {acc['phone']} -> "
                  f"{final.get('status')} {final.get('via', '')}".rstrip() +
                  f" ({str(final.get('error') or '')[:60]})", flush=True)
        if args.delay:
            time.sleep(args.delay)
        return res

    stopped = 0
    aborted = False
    if args.workers > 1:
        # Pull-based scheduling: one lane hitting 429 must stop the *queued*
        # accounts too, exactly like vmax_go (exit 3). Submitting every row up
        # front would keep grinding through the ban and escalate it.
        pending = list(todo)
        take = threading.Lock()
        cursor = {"i": 0}
        stop_ev = threading.Event()

        def next_acc():
            with take:
                if stop_ev.is_set() or cursor["i"] >= len(pending):
                    return None
                acc = pending[cursor["i"]]
                cursor["i"] += 1
                return acc

        def lane():
            while True:
                acc = next_acc()
                if acc is None:
                    return
                if not channel_alive(counters["n"]):
                    stop_ev.set()
                    return
                res = work(acc)
                kind = res.get("register_error_kind") or ""
                if kind == "rate_limited" or "429" in str(res.get("error") or ""):
                    if not stop_ev.is_set():
                        print("[batch] 429 -> letting in-flight accounts finish, then stopping "
                              "(do not poll; each 429 renews the ban)", flush=True)
                    stop_ev.set()

        lanes = [threading.Thread(target=lane, daemon=True) for _ in range(args.workers)]
        for t in lanes:
            t.start()
        for t in lanes:
            t.join()
        if stop_ev.is_set():
            if hw["down"]:
                stopped = 9
            else:
                aborted = True
                stopped = 3
            skipped = len(pending) - cursor["i"]
            if skipped:
                print(f"[batch] {skipped} row(s) not attempted (resume with the same --out)",
                      flush=True)
    else:
        for acc in todo:
            if args.max_minutes and (time.time() - t_start) / 60.0 >= args.max_minutes:
                print(f"[batch] --max-minutes {args.max_minutes:g} reached; stopping", flush=True)
                stopped = 6
                break
            if not channel_alive(counters["n"]):
                stopped = 9
                break
            print(f"[batch] {acc['phone']} proxy={(acc['proxy'] or args.proxy) or 'direct'}", flush=True)
            work(acc)
            if args.max_fail and counters["fail"] >= args.max_fail:
                print(f"[batch] --max-fail {args.max_fail} reached; stopping", flush=True)
                stopped = 5
                break

    dt = time.time() - t_start
    print(f"\n[batch] done in {dt:.0f}s: ok={counters['ok']} reused={counters['reused']} "
          f"fail={counters['fail']} taken={counters['taken']} "
          f"limited={counters['limited']} capped_skipped={len(capped)} aborted={aborted} "
          f"(resume with the same --out)",
          flush=True)
    if reset_state["skipped"]:
        print(f"[batch] {reset_state['skipped']} taken number(s) skipped the reset flow "
              f"(channel closed mid-run) -> they stay for the next run", flush=True)
    if hw["down"]:
        print(f"[batch] stopped on a dead SMS channel after {counters['n']} row(s) "
              f"({hw['detail']}); {hw['probes']} probe(s) this run", flush=True)
    if stopped:
        return stopped
    if counters["fail"]:
        return 2
    return 7 if (counters["taken"] or capped) else 0


if __name__ == "__main__":
    raise SystemExit(main())