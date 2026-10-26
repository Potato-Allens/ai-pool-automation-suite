# -*- coding: utf-8 -*-
"""号源探针：每个号码发一条真短信，只确认「能不能收到」，不建号。

    python vmax_probe.py --input candidates.csv --out probe.jsonl `
      --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=<TEST_SECRET>&since={since}" `
      --captcha queue --queue-dir tokens

为什么单独做这个工具
--------------------
买 100 个号之前，你只需要知道两件事：这个号能不能过运营商校验、能不能真收到码。
完整注册还会消耗一次 `/register` 写配额（约 5 次/60 s，撞了封 200 s+），并且会真的建出账号。
探针只做 `POST /register/send-code` + 等短信，**绝不调用 `/register`**，所以：
  * 不占写配额、不会 429
  * 不建号、不消耗一次「提交表单」的滑块 token
  * 输出里不保存验证码本身（只留长度和掩码），避免日志/截图泄露

输入：CSV（phone,...）/ JSONL / 纯号码一行一个；`#` 开头的行忽略。
退出码：0 全部收到；1 有号失败；2 参数错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()

from vmax_batch import dedupe_accounts, load_accounts  # noqa: E402
from vmax_client import VmaxAuth  # noqa: E402
from vmax_register import (PHONE_RE, VIRTUAL_PREFIXES, is_phone_taken,  # noqa: E402
                           normalize_phone, pop_token)
from vmax_sms import SmsTimeout, build_sms  # noqa: E402


def mask_code(code: str) -> str:
    if not code:
        return ""
    if len(code) <= 2:
        return "*" * len(code)
    return code[0] + "*" * (len(code) - 2) + code[-1]


def probe_one(args, acc: dict) -> dict:
    phone = normalize_phone(acc.get("phone") or "")
    res: dict = {"phone": phone, "ts": int(time.time())}
    if not PHONE_RE.match(phone):
        res.update(status="bad_format", error="need an 11-digit mainland number")
        return res
    if phone[:3] in VIRTUAL_PREFIXES and not args.allow_virtual:
        res.update(status="virtual_range",
                   error=f"{phone[:3]} is a virtual operator range; the server rejects it")
        return res

    cli = VmaxAuth(proxy=acc.get("proxy") or args.proxy,
                   base=args.base, redirect_uri=args.redirect)
    t0 = time.time()
    try:
        param = (args.captcha_param if args.captcha == "param"
                 else pop_token(args.queue_dir, args.queue_wait))
    except RuntimeError as e:
        res.update(status="no_token", error=str(e)[:300])
        return res
    res["captcha_seconds"] = round(time.time() - t0, 1)

    t_send = time.time()
    try:
        r = cli.send_register_code(phone, param, client=args.platform)
    except Exception as e:  # noqa: BLE001 - one bad number must not kill the probe
        res.update(status="send_error", error=f"{type(e).__name__}: {e}"[:300])
        return res
    res["send_code"] = r
    if not r.get("ok"):
        msg = str(r.get("message") or "")
        if is_phone_taken(msg):
            # The number already has an account: no SMS was sent, no /register
            # write happened.  Useful signal for a pool you are about to buy.
            res.update(status="already_registered", error=msg[:200])
            return res
        res.update(status="send_rejected", error=msg[:200])
        return res

    try:
        code = build_sms(args.sms, args.sms_dir).wait(phone, timeout=args.sms_timeout,
                                                      since=t_send)
    except (SmsTimeout, RuntimeError) as e:
        res.update(status="sms_timeout", error=str(e)[:300],
                   sms_seconds=round(time.time() - t_send, 1))
        return res
    res.update(status="ok", sms_seconds=round(time.time() - t_send, 1),
               code_len=len(code), code_masked=mask_code(code))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="probe whether a phone can receive the code (no account)")
    ap.add_argument("--input", required=True, help="CSV / JSONL / one phone per line")
    ap.add_argument("--out", default="probe.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1, help="parallel probes (token queue is safe)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between numbers (per worker)")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop after N minutes")
    ap.add_argument("--captcha", default="queue", choices=["queue", "param"])
    ap.add_argument("--captcha-param", default="")
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=600)
    ap.add_argument("--sms", default="none",
                    help="http: / file / cmd: / none -- same spec as vmax_register")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-timeout", type=int, default=180)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--allow-virtual", action="store_true")
    args = ap.parse_args(argv)

    if args.captcha == "param" and not args.captcha_param:
        print("[probe] --captcha param needs --captcha-param", file=sys.stderr)
        return 2
    if not os.path.exists(args.input):
        print(f"[probe] no input at {args.input}", file=sys.stderr)
        return 2

    accounts, dups = dedupe_accounts(load_accounts(args.input))
    if dups:
        print(f"[probe] collapsed {dups} duplicate phone row(s)", flush=True)
    todo = accounts[: args.limit] if args.limit else accounts
    if not todo:
        print("[probe] nothing to probe", file=sys.stderr)
        return 2
    if args.workers < 1:
        args.workers = 1

    lock = threading.Lock()
    counters = {"ok": 0, "fail": 0, "n": 0}
    by_status: dict[str, int] = {}
    started = time.time()

    def work(acc: dict) -> dict:
        res = probe_one(args, acc)
        with lock:
            with open(args.out, "a", encoding="utf-8") as f:
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
            counters["n"] += 1
            st = str(res.get("status"))
            by_status[st] = by_status.get(st, 0) + 1
            if st == "ok":
                counters["ok"] += 1
            else:
                counters["fail"] += 1
            extra = (f"{res.get('sms_seconds')}s" if st == "ok"
                     else str(res.get("error") or "")[:70])
            print(f"[probe] {counters['n']}/{len(todo)} {res['phone']} {st} {extra}",
                  flush=True)
        if args.delay:
            time.sleep(args.delay)
        return res

    stopped = 0
    if args.workers > 1:
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
                work(acc)
                if args.max_minutes and (time.time() - started) / 60.0 >= args.max_minutes:
                    stop_ev.set()

        lanes = [threading.Thread(target=lane, daemon=True) for _ in range(args.workers)]
        for t in lanes:
            t.start()
        for t in lanes:
            t.join()
        if stop_ev.is_set() and cursor["i"] < len(pending):
            stopped = 6
    else:
        for acc in todo:
            if args.max_minutes and (time.time() - started) / 60.0 >= args.max_minutes:
                stopped = 6
                break
            work(acc)

    summary = {
        "ts": int(time.time()),
        "elapsed_s": round(time.time() - started, 1),
        "input": os.path.abspath(args.input),
        "out": os.path.abspath(args.out),
        "rows": len(todo),
        "ok": counters["ok"],
        "fail": counters["fail"],
        "by_status": by_status,
        "stopped_on_max_minutes": bool(stopped),
        "note": "one real SMS per number; POST /register is never called; the code itself is not stored",
    }
    sp = args.out + ".summary.json"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"[probe] ok={counters['ok']} fail={counters['fail']} in {summary['elapsed_s']}s -> {sp}",
          flush=True)
    if stopped:
        return stopped
    return 0 if counters["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
