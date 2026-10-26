# -*- coding: utf-8 -*-
"""Summarise a results.jsonl from vmax_register / vmax_batch / vmax_probe.

    python vmax_report.py results.jsonl
    python vmax_report.py results.jsonl --json

Reports: rows, unique phones, ok / error / taken, success rate, throughput, and
the top normalised error buckets (phone numbers / ids / paths stripped).

Why ``taken`` is its own bucket
-------------------------------
A number that is already registered is refused at ``/register/send-code`` (openapi:
"向未注册手机号发送"), so it costs **one captcha token, zero SMS, zero /register
writes** and the runner skips it on every later run (exit 7).  Counting it as an
error would make a healthy run look broken and would drag the success rate down
for a row that never really attempted anything -- hence:

  * ``attempted = ok + error`` (taken rows never attempted a registration)
  * ``success_rate = ok / attempted``
  * ``taken`` printed separately, with the "0 SMS / terminal" reminder

Accepts both runners' rows (``register_error_kind=phone_taken``) and probe rows
(``status=already_registered``).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter

PHONE_RE = re.compile(r"1[3-9]\d{9}")
HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b")
PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]+")
QUOTED_RE = re.compile(r"'[^']{1,40}'")

TAKEN_KINDS = ("phone_taken",)
TAKEN_STATUSES = ("already_registered",)


def normalize(err: str) -> str:
    s = str(err or "").strip()
    for prefix in ("VmaxError: ", "RegisterError: ", "RuntimeError: ", "SmsTimeout: "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = PHONE_RE.sub("<phone>", s)
    s = PATH_RE.sub("<path>", s)
    s = HEX_RE.sub("<hex>", s)
    s = QUOTED_RE.sub("<val>", s)
    s = re.sub(r"\s+", " ", s)
    return s[:120]


def is_taken(r: dict) -> bool:
    """True when the server said this number already has an account.

    Terminal, costs no SMS, and must not be reported as a failure: a runner that
    stops on ``--max-fail`` would otherwise abort a healthy recycled-number pool.
    """
    if r.get("status") == "ok":
        return False
    if r.get("status") in TAKEN_STATUSES:
        return True
    return r.get("register_error_kind") in TAKEN_KINDS


def summarise(rows: list[dict]) -> dict:
    phones = [str(r.get("phone") or "") for r in rows]
    unique = set(p for p in phones if p)
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    taken_rows = [r for r in rows if is_taken(r)]
    # A failed password reset is not a failed registration: the number never got
    # near /register (it was already taken).  Counting it as an error would make
    # a recycled-number pool with a daily cap look like a broken run.
    reset_rows = [r for r in rows if r.get("status") != "ok" and r.get("reset_error_kind")]
    reset_ids = {id(r) for r in reset_rows}
    err_rows = [r for r in rows
                if r.get("status") != "ok" and not is_taken(r) and id(r) not in reset_ids]
    buckets = Counter(normalize(str(r.get("error", ""))) for r in err_rows)

    ts = [float(r["ts"]) for r in rows
          if isinstance(r.get("ts"), (int, float)) and not isinstance(r.get("ts"), bool)]
    span = (max(ts) - min(ts)) if len(ts) > 1 else 0.0
    rate = (len(rows) / (span / 3600)) if span else 0.0
    attempted = len(ok_rows) + len(err_rows)
    # A recovered number (password reset) also lands as status=ok, but it is not
    # a registration: keep the two visible separately.
    reused = len([r for r in ok_rows if r.get("via") == "reset"])
    return {
        "rows": len(rows),
        "unique_phones": len(unique),
        "duplicates": len(phones) - len(unique),
        "ok": len(ok_rows),
        "reused": reused,
        "error": len(err_rows),
        "taken": len(taken_rows),
        "reset_failed": len(reset_rows),
        "limited": len([r for r in reset_rows
                        if r.get("reset_error_kind") == "daily_limit"]),
        "attempted": attempted,
        "success_rate": round(len(ok_rows) / attempted, 3) if attempted else 0.0,
        "span_seconds": round(span, 1),
        "rows_per_hour": round(rate, 1),
        "top_errors": buckets.most_common(10),
    }


def load_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    # utf-8-sig: a results file that went through Excel / a BOM-writing editor
    # would otherwise lose its first row to a JSONDecodeError.
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="summarise results.jsonl")
    ap.add_argument("path")
    ap.add_argument("--json", action="store_true", help="machine readable summary")
    args = ap.parse_args(argv)

    try:
        rows = load_rows(args.path)
    except OSError as e:
        print(f"[report] cannot read {args.path}: {e}", flush=True)
        return 2

    s = summarise(rows)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0

    print(f"rows={s['rows']} unique={s['unique_phones']} dup={s['duplicates']} "
          f"ok={s['ok']} reused={s['reused']} error={s['error']} taken={s['taken']} "
          f"rate={s['success_rate']:.1%} (ok/attempted) "
          f"throughput={s['rows_per_hour']}/h")
    if s["taken"]:
        print(f"note: {s['taken']} phone(s) already registered elsewhere "
              f"(0 SMS, terminal; resume skips them) -- not counted as error")
    if s["reset_failed"]:
        print(f"note: {s['reset_failed']} reset attempt(s) failed "
              f"(limited={s['limited']}); not counted as registration errors")
    if s["error"]:
        print("\ntop errors:")
        for msg, n in s["top_errors"]:
            print(f"  {n:5}  {msg or '(empty)'}")
    elif s["attempted"]:
        print("\nno errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
