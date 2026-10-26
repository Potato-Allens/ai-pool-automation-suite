# -*- coding: utf-8 -*-
"""vmax_report: results.jsonl summary must tell taken apart from failed.

A number that is already registered costs 1 captcha token, 0 SMS and 0 /register
writes, and every later run skips it.  Reporting it as an error makes a healthy
run look broken (and drags success_rate down for a row that never attempted).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vmax_report as RP  # noqa: E402

PY = sys.executable
fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def write(path, rows, bom=False):
    enc = "utf-8-sig" if bom else "utf-8"
    with open(path, "w", encoding=enc) as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


def run(path, *extra):
    return subprocess.run([PY, "vmax_report.py", path, *extra], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="report_")
    print("[1] mixed batch: ok / real error / phone_taken")
    rows = [
        {"phone": "13500135001", "status": "ok", "uid": "u1", "ts": 1000},
        {"phone": "13500135002", "status": "ok", "uid": "u2", "ts": 1001},
        {"phone": "13500135003", "status": "ok", "uid": "u3", "ts": 1002},
        {"phone": "13500135004", "status": "error", "ts": 1003,
         "register_error_kind": "phone_taken",
         "error": "VmaxError: send-code rejected: 该手机号已注册，请直接登录"},
        {"phone": "13500135005", "status": "error", "ts": 1004,
         "register_error_kind": "code_wrong",
         "error": "VmaxError: register failed: 验证码不正确"},
    ]
    p = os.path.join(tmp, "results.jsonl")
    write(p, rows)
    s = RP.summarise(RP.load_rows(p))
    check("rows", s["rows"] == 5, s)
    check("ok=3", s["ok"] == 3, s)
    check("taken=1", s["taken"] == 1, s)
    check("error=1 (taken excluded)", s["error"] == 1, s)
    check("attempted = ok + error", s["attempted"] == 4, s)
    check("success_rate over attempted = 0.75", s["success_rate"] == 0.75, s)
    check("taken is not in top_errors",
          all("已注册" not in msg for msg, _ in s["top_errors"]), s["top_errors"])
    check("the real error is in top_errors",
          any("验证码不正确" in msg for msg, _ in s["top_errors"]), s["top_errors"])

    print("[2] human output names taken and explains it")
    r = run(p)
    check("exit 0", r.returncode == 0, r.stderr[-300:])
    check("headline has ok/error/taken", "ok=3" in r.stdout and "error=1" in r.stdout
          and "taken=1" in r.stdout, r.stdout)
    check("says 0 SMS / terminal", "0 SMS" in r.stdout and "terminal" in r.stdout, r.stdout)
    check("rate labelled ok/attempted", "ok/attempted" in r.stdout, r.stdout)

    print("[3] --json shape")
    r = run(p, "--json")
    j = json.loads(r.stdout)
    check("json exit 0", r.returncode == 0, r.stderr[-200:])
    check("json has taken/attempted", j.get("taken") == 1 and j.get("attempted") == 4, j)
    check("json success_rate 0.75", j.get("success_rate") == 0.75, j)
    check("json top_errors is a list", isinstance(j.get("top_errors"), list), j)

    print("[4] probe rows use status=already_registered")
    p2 = os.path.join(tmp, "probe.jsonl")
    write(p2, [{"phone": "13500135010", "status": "already_registered", "ts": 10,
                "error": "该手机号已注册"},
               {"phone": "13500135011", "status": "ok", "ts": 11}])
    s = RP.summarise(RP.load_rows(p2))
    check("probe taken counted", s["taken"] == 1 and s["error"] == 0, s)
    check("probe rate 1/1", s["success_rate"] == 1.0, s)

    print("[5] all-taken pool does not divide by zero")
    p3 = os.path.join(tmp, "taken.jsonl")
    write(p3, [{"phone": f"1350013502{i}", "status": "error", "ts": 20 + i,
                "register_error_kind": "phone_taken"} for i in range(3)])
    s = RP.summarise(RP.load_rows(p3))
    check("attempted 0", s["attempted"] == 0, s)
    check("rate 0.0 (not a crash)", s["success_rate"] == 0.0, s)
    check("taken 3", s["taken"] == 3, s)
    r = run(p3)
    check("exit 0 with a full taken pool", r.returncode == 0, r.stdout)

    print("[6] junk, non-dict lines and a BOM do not eat the file")
    p4 = os.path.join(tmp, "messy.jsonl")
    write(p4, ["not json", "123", "[1, 2]", "",
               {"phone": "13500135030", "status": "ok", "ts": 30}], bom=True)
    rows4 = RP.load_rows(p4)
    check("only the one dict survives", len(rows4) == 1, rows4)
    check("BOM did not kill the first data row", rows4 and rows4[0]["phone"] == "13500135030",
          rows4)
    r = run(p4)
    check("messy file still exits 0", r.returncode == 0, r.stderr[-200:])
    check("messy file reports 1 row", "rows=1" in r.stdout, r.stdout)

    print("[7] float ts (vmax_smsd style) still gives a throughput")
    p5 = os.path.join(tmp, "float_ts.jsonl")
    write(p5, [{"phone": "13500135041", "status": "ok", "ts": 1000.5},
               {"phone": "13500135042", "status": "ok", "ts": 1360.5}])
    s = RP.summarise(RP.load_rows(p5))
    check("span from float ts = 360s", s["span_seconds"] == 360.0, s)
    check("throughput 20/h", s["rows_per_hour"] == 20.0, s)

    print("[8] empty file and missing file")
    p6 = os.path.join(tmp, "empty.jsonl")
    open(p6, "w", encoding="utf-8").close()
    r = run(p6)
    check("empty -> exit 0", r.returncode == 0, r.stderr[-200:])
    check("empty -> rows=0", "rows=0" in r.stdout, r.stdout)
    r = run(os.path.join(tmp, "nope.jsonl"))
    check("missing file -> exit 2", r.returncode == 2, r.returncode)
    check("missing file says why", "cannot read" in r.stdout, r.stdout)

    print("[9] normalisation still strips phones / paths / hex")
    check("phone masked", RP.normalize("发码失败 13500135001") == "发码失败 <phone>",
          RP.normalize("发码失败 13500135001"))
    check("path masked", "<path>" in RP.normalize(r"C:\Users\x\y.json not found"),
          RP.normalize(r"C:\Users\x\y.json not found"))
    check("prefix stripped", RP.normalize("VmaxError: boom") == "boom")

    print()
    if fails:
        print(f"FAILED {len(fails)}: " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
