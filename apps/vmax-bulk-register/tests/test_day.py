# -*- coding: utf-8 -*-
"""Offline checks for the day-of runner (no network, no browsers, no SMS)."""
from __future__ import annotations

import csv
import json
import os
import pathlib
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import vmax_day as D  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def write_cards(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone", "password"])
        for r in rows:
            w.writerow(r)
    return path


class StubFarm:
    instances: list["StubFarm"] = []

    def __init__(self, cmd, token_dir, log_path, **kw):
        self.cmd = cmd
        self.token_dir = token_dir
        self.log_path = log_path
        self.starts = 0
        self.restarts = 0
        self.dead = False
        self.stopped = 0
        StubFarm.instances.append(self)

    def start(self):
        self.starts += 1
        return True

    def wait_ready(self, want=1, timeout=0):
        return True

    def check(self):
        pass

    def stop(self):
        self.stopped += 1


class FakeProc:
    def __init__(self, alive=True, pid=4242):
        self.alive = alive
        self.pid = pid
        self.killed = False

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        self.alive = False
        return 0


def step_name(cmd):
    tool = os.path.basename(cmd[2]) if len(cmd) > 2 else ""
    if tool == "vmax_go.py":
        return "preflight" if "--preflight-only" in cmd else "batch"
    if tool == "vmax_login.py":
        return "verify"
    if tool == "vmax_report.py":
        return "report_login" if any(a.endswith("logins.jsonl") for a in cmd[3:]) else "report"
    if tool == "vmax_tokens.py":
        return "farm"
    if tool == "vmax_contract.py":
        return "contract"
    return "other"


def flag_value(cmd, name):
    """Value that follows *name* in a command line, or None."""
    try:
        return cmd[cmd.index(name) + 1]
    except (ValueError, IndexError):
        return None


def fake_execute(rcs, out_path, ok_rows=2):
    """Build an _execute replacement that returns scripted exit codes."""
    calls = []

    def ex(cmd, log_path, echo=True):
        name = step_name(cmd)
        calls.append(name)
        os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("# " + " ".join(cmd) + "\n")
        if name == "batch" and "results.jsonl" in out_path.name:
            pass
        target = out_path if name == "batch" else out_path.with_name("logins.jsonl")
        if name == "batch":
            with open(str(target) + ".summary.json", "w", encoding="utf-8") as f:
                json.dump({"ok": ok_rows, "fail": 0, "taken": 0}, f)
        if name == "verify":
            with open(str(target) + ".summary.json", "w", encoding="utf-8") as f:
                json.dump({"ok": ok_rows, "fail": 0}, f)
        return rcs.get(name, 0), f"{name} fake output\n"

    return ex, calls


def read_manifest(run_dir):
    with open(os.path.join(run_dir, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    print("[1] secrets never reach a log, a command line or the manifest")
    check("query secret masked", D.mask_secrets("a?phone=1&secret=<TEST_SECRET>&since=2")
          == "a?phone=1&secret=***&since=2")
    check("header secret masked", D.mask_secrets("X-Sms-Secret: abc123") == "X-Sms-Secret: ***")
    check("normal text untouched", D.mask_secrets("phone=13800138000") == "phone=13800138000")

    print("[2] row counting")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138001", "pw1"),
                                                           ("13800138002", "pw2")])
        check("csv rows minus header", D.count_rows(cards) == 2, D.count_rows(cards))
        jl = os.path.join(td, "rows.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            f.write('{"phone": "1"}\n\n{"phone": "2"}\n')
        check("jsonl ignores blanks", D.count_rows(jl) == 2, D.count_rows(jl))
        check("missing file -> 0", D.count_rows(os.path.join(td, "nope.csv")) == 0)

    print("[3] dry run writes a manifest and touches nothing else")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"),
                            [("13800138001", "pw1"), ("13800138002", "pw2")])
        run_dir = os.path.join(td, "run")
        out = os.path.join(run_dir, "results.jsonl")
        rc = D.main(["--dry-run", "--input", cards, "--out", out,
                     "--sms", "http://127.0.0.1:8787/code?phone={phone}&secret=<TEST_SECRET>"])
        man = read_manifest(run_dir)
        check("exit 0", rc == 0, rc)
        check("verdict dry_run", man["verdict"] == "dry_run", man.get("verdict"))
        check("input hashed", len(man["input"]["sha256"]) == 64, man["input"]["sha256"])
        check("input rows counted", man["input"]["rows"] == 2, man["input"]["rows"])
        check("plan records five steps", len(man["plan"]) == 6, len(man["plan"]))
        check("plan masks the sms secret",
              "<TEST_SECRET>" not in json.dumps(man["plan"], ensure_ascii=False))
        check("manifest masks the sms secret",
              "<TEST_SECRET>" not in json.dumps(man["config"], ensure_ascii=False))
        check("no helper keys on disk", "_path" not in man and "_t0" not in man)
        check("results file not created", not os.path.exists(out))
        check("no steps ran", man["steps"] == [])

    print("[3b] --retry-limited reaches the batch step, and only with --on-taken reset")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138001", "pw1")])
        run_dir = os.path.join(td, "run")
        out = os.path.join(run_dir, "results.jsonl")
        rc = D.main(["--dry-run", "--input", cards, "--out", out,
                     "--on-taken", "reset", "--retry-limited"])
        man = read_manifest(run_dir)
        batch = man["plan"].get("batch") or []
        check("dry run ok", rc == 0, rc)
        check("manifest records the flag", man["config"].get("retry_limited") is True,
              man["config"].get("retry_limited"))
        check("batch carries --retry-limited",
              "--retry-limited" in batch, batch)
        run_dir2 = os.path.join(td, "run2")
        out2 = os.path.join(run_dir2, "results.jsonl")
        rc = D.main(["--dry-run", "--input", cards, "--out", out2, "--on-taken", "reset"])
        man2 = read_manifest(run_dir2)
        batch2 = man2["plan"].get("batch") or []
        check("absent unless asked",
              bool(batch2) and "--retry-limited" not in batch2, batch2)

    print("[4] happy path: preflight -> farm -> batch -> verify -> report")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"),
                            [("13800138001", "pw1"), ("13800138002", "pw2")])
        run_dir = os.path.join(td, "run")
        out = os.path.join(run_dir, "results.jsonl")
        StubFarm.instances.clear()
        old_farm = D.Farm
        D.Farm = StubFarm
        ex, calls = fake_execute({}, pathlib.Path(out))
        old_exec = D._execute
        D._execute = ex
        try:
            rc = D.main(["--input", cards, "--out", out, "--yes"])
        finally:
            D.Farm = old_farm
            D._execute = old_exec
        man = read_manifest(run_dir)
        check("exit 0", rc == 0, rc)
        check("verdict ok", man["verdict"] == "ok", man.get("verdict"))
        check("step order", calls == ["preflight", "batch", "verify", "report", "report_login"],
              calls)
        check("farm started once", StubFarm.instances and StubFarm.instances[0].starts == 1)
        check("farm stopped", StubFarm.instances and StubFarm.instances[0].stopped == 1)
        check("farm recorded ready", man["farm"].get("ready") is True, man.get("farm"))
        check("batch summary attached", man["batch_summary"]["ok"] == 2)
        check("login summary attached", man["login_summary"]["ok"] == 2)
        check("logs written", os.path.isdir(os.path.join(run_dir, "logs")))
        check("each step has a log path",
              all(s.get("log") for s in man["steps"]) and len(man["steps"]) == 5,
              len(man["steps"]))

    print("[5] preflight failure stops before any token/SMS spend")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138001", "pw1")])
        run_dir = os.path.join(td, "run")
        out = os.path.join(run_dir, "results.jsonl")
        ex, calls = fake_execute({"preflight": 2}, pathlib.Path(out))
        old_exec, old_farm = D._execute, D.Farm
        D._execute, D.Farm = ex, StubFarm
        StubFarm.instances.clear()
        try:
            rc = D.main(["--input", cards, "--out", out])
        finally:
            D._execute, D.Farm = old_exec, old_farm
        man = read_manifest(run_dir)
        check("exit 2", rc == 2, rc)
        check("verdict preflight_failed", man["verdict"] == "preflight_failed")
        check("batch never ran", calls == ["preflight"], calls)
        check("farm never started", StubFarm.instances == [], StubFarm.instances)

    print("[6] --force lets a failing preflight through")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138001", "pw1")])
        out = os.path.join(td, "run", "results.jsonl")
        ex, calls = fake_execute({"preflight": 2}, pathlib.Path(out))
        old_exec, old_farm = D._execute, D.Farm
        D._execute, D.Farm = ex, StubFarm
        try:
            rc = D.main(["--input", cards, "--out", out, "--force", "--yes"])
        finally:
            D._execute, D.Farm = old_exec, old_farm
        check("keeps going", calls[:2] == ["preflight", "batch"], calls)
        check("exit follows the batch", rc == 0, rc)

    print("[7] batch exit codes keep their meaning")
    cases = [(3, "aborted_429"), (7, "pool_had_taken"), (4, "nothing_to_do"),
             (5, "stopped_max_fail"), (6, "stopped_max_minutes"), (1, "some_failed")]
    for code, want in cases:
        with tempfile.TemporaryDirectory() as td:
            cards = write_cards(os.path.join(td, "cards.csv"), [("13800138001", "pw1")])
            out = os.path.join(td, "run", "results.jsonl")
            ex, calls = fake_execute({"batch": code}, pathlib.Path(out))
            old_exec, old_farm = D._execute, D.Farm
            D._execute, D.Farm = ex, StubFarm
            try:
                rc = D.main(["--input", cards, "--out", out, "--yes"])
            finally:
                D._execute, D.Farm = old_exec, old_farm
            man = read_manifest(os.path.join(td, "run"))
            check(f"batch {code} -> exit {code}", rc == code, rc)
            check(f"batch {code} -> {want}", man["verdict"] == want, man["verdict"])

    print("[8] verify is skipped when nothing registered")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138001", "pw1")])
        out = os.path.join(td, "run", "results.jsonl")
        ex, calls = fake_execute({}, pathlib.Path(out), ok_rows=0)
        old_exec, old_farm = D._execute, D.Farm
        D._execute, D.Farm = ex, StubFarm
        try:
            rc = D.main(["--input", cards, "--out", out, "--yes"])
        finally:
            D._execute, D.Farm = old_exec, old_farm
        check("no verify step", "verify" not in calls, calls)
        check("still reports", calls[-1] == "report", calls)
        check("exit 0", rc == 0, rc)

    print("[9] a failed re-check is not 'ok'")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138001", "pw1")])
        out = os.path.join(td, "run", "results.jsonl")
        ex, calls = fake_execute({"verify": 1}, pathlib.Path(out))
        old_exec, old_farm = D._execute, D.Farm
        D._execute, D.Farm = ex, StubFarm
        try:
            rc = D.main(["--input", cards, "--out", out, "--yes"])
        finally:
            D._execute, D.Farm = old_exec, old_farm
        man = read_manifest(os.path.join(td, "run"))
        check("verify_failed", man["verdict"] == "verify_failed", man["verdict"])
        check("exit 1", rc == 1, rc)

    print("[10] bad inputs are rejected before anything is spent")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "run", "results.jsonl")
        rc = D.main(["--input", os.path.join(td, "missing.csv"), "--out", out])
        check("missing input -> 2", rc == 2, rc)
        empty = write_cards(os.path.join(td, "empty.csv"), [])
        rc = D.main(["--input", empty, "--out", os.path.join(td, "run2", "r.jsonl")])
        check("empty input -> 2", rc == 2, rc)
        man = read_manifest(os.path.join(td, "run2"))
        check("empty_input verdict", man["verdict"] == "empty_input", man["verdict"])

    print("[11] farm supervision")
    with tempfile.TemporaryDirectory() as td:
        procs = []

        def fake_popen(cmd, fh):
            p = FakeProc()
            procs.append(p)
            return p

        farm = D.Farm(["python", "x.py"], td, os.path.join(td, "farm.log"),
                      popen=fake_popen, restart_limit=2)
        check("start ok", farm.start() is True)
        check("alive while running", farm.alive() is True)
        open(os.path.join(td, "tok_1.json"), "w").close()
        open(os.path.join(td, "tok_2.json"), "w").close()
        open(os.path.join(td, "tok_1.json.used"), "w").close()
        open(os.path.join(td, "tok_3.json.stale"), "w").close()
        check("depth counts only pending tokens", farm.depth() == 2, farm.depth())
        farm.proc.alive = False
        farm.check()
        check("dead farm is restarted", farm.restarts == 1 and farm.starts == 2,
              (farm.restarts, farm.starts))
        farm.proc.alive = False
        farm.check()
        farm.proc.alive = False
        farm.check()
        check("restarts are capped", farm.dead is True and farm.restarts == 2,
              (farm.dead, farm.restarts))
        farm.stop()
        check("stop does not raise", farm.proc is not None)

    with tempfile.TemporaryDirectory() as td:
        def dead_popen(cmd, fh):
            return FakeProc(alive=False)

        farm = D.Farm(["python", "x.py"], td, os.path.join(td, "farm.log"),
                      popen=dead_popen, restart_limit=1)
        farm.start()
        t0 = time.time()
        ready = farm.wait_ready(want=1, timeout=2)
        check("no token -> not ready", ready is False)
        check("wait_ready respects the timeout", time.time() - t0 < 12)
        farm.stop()  # closes the log handle (Windows cannot delete it otherwise)

    print("[12] farm that cannot even start")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138001", "pw1")])
        out = os.path.join(td, "run", "results.jsonl")

        class BrokenFarm(StubFarm):
            def start(self):
                self.starts += 1
                return False

        old_farm, old_exec = D.Farm, D._execute
        D.Farm = BrokenFarm
        D._execute = lambda cmd, log_path, echo=True: (0, "")
        try:
            rc = D.main(["--input", cards, "--out", out, "--skip-preflight"])
        finally:
            D.Farm, D._execute = old_farm, old_exec
        man = read_manifest(os.path.join(td, "run"))
        check("exit 8", rc == 8, rc)
        check("farm_failed verdict", man["verdict"] == "farm_failed", man["verdict"])

    print("[13] SMS channel probe (0 token, 0 SMS)")
    import http.server
    import threading as _threading

    class ProbeHandler(http.server.BaseHTTPRequestHandler):
        mode = "ok"

        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/health"):
                if ProbeHandler.mode == "ok":
                    return self._send(200, {"ok": True, "stored": 2,
                                            "modems": [{"name": "COM3", "connected": True}]})
                if ProbeHandler.mode == "down_modem":
                    return self._send(200, {"ok": True,
                                            "modems": [{"name": "COM3", "connected": False}]})
                if ProbeHandler.mode == "unauthorised":
                    return self._send(401, {"ok": False})
                return self._send(404, {"ok": False})
            if ProbeHandler.mode == "old_relay":
                return self._send(404, {"ok": False, "code": None})
            return self._send(200, {"ok": True, "code": None})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
    port = srv.server_address[1]
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/code?phone={phone}&secret=<TEST_SECRET>" % port
    try:
        ProbeHandler.mode = "ok"
        ok, detail = D.build_sms(url).probe()
        check("health 200 + modem up", ok and "COM3:up" in detail, detail)
        check("health detail includes the queue", "stored=2" in detail, detail)
        ProbeHandler.mode = "down_modem"
        ok, detail = D.build_sms(url).probe()
        check("all modems down -> not ready", not ok and "no modem connected" in detail, detail)
        ProbeHandler.mode = "unauthorised"
        ok, detail = D.build_sms(url).probe()
        check("401 -> not ready", not ok and "401" in detail, detail)
        ProbeHandler.mode = "old_relay"
        ok, detail = D.build_sms(url).probe()
        check("no /health falls back to a sentinel fetch",
              ok and "sentinel -> 404" in detail, detail)
    finally:
        srv.shutdown()
        srv.server_close()

    ok, detail = D.build_sms("http:http://127.0.0.1:1/code?phone={phone}").probe()
    check("dead endpoint -> not ready", not ok, detail)
    with tempfile.TemporaryDirectory() as td:
        check("file provider: dir exists", D.build_sms("file:local", td).probe()[0])
        ok, detail = D.build_sms("file:local", os.path.join(td, "nope")).probe()
        check("file provider: missing dir -> not ready",
              not ok and "does not exist" in detail, detail)
    ok, detail = D.build_sms("none").probe()
    check("`none` provider -> not ready", not ok, detail)
    # channel_check is the one vocabulary day / go / batch all use; it must
    # never raise, and it must name the provider it failed to build.
    chk = D.channel_check("http:http://127.0.0.1:1/code?phone={phone}")
    check("channel_check: dead endpoint -> down, HttpSms",
          chk["ok"] is False and chk["provider"] == "HttpSms", chk)
    with tempfile.TemporaryDirectory() as td:
        chk = D.channel_check("file:local", td)
        check("channel_check: existing dir -> up, FileSms",
              chk["ok"] is True and chk["provider"] == "FileSms", chk)
    chk = D.channel_check("cmd:no-such-binary-xyz")
    check("channel_check: a bad spec is a verdict, not an exception",
          chk["ok"] is False and chk["provider"] == "CommandSms", chk)
    check("cmd provider finds the interpreter",
          D.build_sms("cmd:" + sys.executable + " -V").probe()[0])
    check("cmd provider flags a missing binary",
          not D.build_sms("cmd:no-such-binary-xyz").probe()[0])

    print("[14] a dead SMS channel stops the day before the farm starts")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138009", "pw9")])
        run_dir = os.path.join(td, "run")
        out = os.path.join(run_dir, "results.jsonl")
        StubFarm.instances.clear()
        calls = []
        old_farm, old_exec = D.Farm, D._execute
        D.Farm = StubFarm
        D._execute = lambda cmd, log_path, echo=True: (calls.append(cmd[1].split("\\")[-1]), (0, ""))[1]
        try:
            rc = D.main(["--input", cards, "--out", out, "--skip-preflight",
                         "--sms", "http:http://127.0.0.1:1/code?phone={phone}"])
        finally:
            D.Farm, D._execute = old_farm, old_exec
        man = read_manifest(run_dir)
        check("exit 9", rc == 9, rc)
        check("sms_channel_down verdict", man["verdict"] == "sms_channel_down", man["verdict"])
        check("no farm instance was created", StubFarm.instances == [], StubFarm.instances)
        check("no step ran", man["steps"] == [], man["steps"])
        check("manifest records the failed probe",
              man["sms_check"]["ok"] is False and "unreachable" in man["sms_check"]["detail"],
              man.get("sms_check"))
        check("probe is marked as requested", man["config"]["sms_check"] is True,
              man["config"].get("sms_check"))

        run_dir2 = os.path.join(td, "run2")
        out2 = os.path.join(run_dir2, "results.jsonl")
        old_farm, old_exec = D.Farm, D._execute
        D.Farm, D._execute = StubFarm, lambda cmd, log_path, echo=True: (0, "")
        try:
            rc = D.main(["--input", cards, "--out", out2, "--skip-preflight", "--preflight-only",
                         "--sms", "http:http://127.0.0.1:1/code?phone={phone}",
                         "--no-sms-check"])
        finally:
            D.Farm, D._execute = old_farm, old_exec
        man2 = read_manifest(run_dir2)
        check("--no-sms-check bypasses the gate",
              rc == 0 and man2["verdict"] == "preflight_only", (rc, man2["verdict"]))
        check("bypass records that the check was skipped",
              man2["config"]["sms_check"] is False, man2["config"].get("sms_check"))

        run_dir3 = os.path.join(td, "run3")
        out3 = os.path.join(run_dir3, "results.jsonl")
        old_farm, old_exec = D.Farm, D._execute
        D.Farm, D._execute = StubFarm, lambda cmd, log_path, echo=True: (0, "")
        try:
            rc = D.main(["--input", cards, "--out", out3, "--skip-preflight", "--preflight-only",
                         "--sms", "http:http://127.0.0.1:1/code?phone={phone}", "--force"])
        finally:
            D.Farm, D._execute = old_farm, old_exec
        man3 = read_manifest(run_dir3)
        check("--force keeps going and still records the probe",
              rc == 0 and man3["verdict"] == "preflight_only"
              and man3["sms_check"]["ok"] is False, (rc, man3.get("sms_check")))

    print("[15] a mid-run SMS stop keeps the finished rows and still exits 9")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138012", "pw")])
        out = os.path.join(td, "run", "results.jsonl")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"phone": "13800138012", "status": "ok"}) + "\n")
        ex, calls = fake_execute({"batch": 9}, pathlib.Path(out), ok_rows=1)
        old_exec, old_farm = D._execute, D.Farm
        D._execute, D.Farm = ex, StubFarm
        try:
            rc = D.main(["--input", cards, "--out", out, "--skip-preflight",
                         "--skip-farm", "--no-sms-check", "--yes"])
        finally:
            D._execute, D.Farm = old_exec, old_farm
        man = read_manifest(os.path.join(td, "run"))
        check("exit 9", rc == 9, rc)
        check("verdict sms_channel_down", man["verdict"] == "sms_channel_down", man["verdict"])
        check("the finished rows are still verified", "verify" in calls, calls)
        check("and still reported", "report" in calls, calls)

    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138013", "pw")])
        out = os.path.join(td, "run", "results.jsonl")
        ex, calls = fake_execute({"batch": 9}, pathlib.Path(out))
        old_exec, old_farm = D._execute, D.Farm
        D._execute, D.Farm = ex, StubFarm
        try:
            rc = D.main(["--input", cards, "--out", out, "--skip-preflight",
                         "--skip-farm", "--no-sms-check", "--yes"])
        finally:
            D._execute, D.Farm = old_exec, old_farm
        check("no rows -> exit 9", rc == 9, rc)
        check("no rows -> report is skipped", "report" not in calls, calls)

    print("[16] the day hands the mid-run watchdog to the child")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138021", "pw")])
        old_exec, old_farm = D._execute, D.Farm
        D.Farm = StubFarm
        seen = {}

        def capture(script):
            def ex(cmd, log_path, echo=True):
                seen[step_name(cmd)] = list(cmd)
                return script(cmd, log_path, echo)
            return ex

        try:
            out_a = os.path.join(td, "a", "results.jsonl")
            ex_a, _ = fake_execute({}, pathlib.Path(out_a))
            D._execute = capture(ex_a)
            rc = D.main(["--input", cards, "--out", out_a, "--yes"])
            batch = seen.get("batch") or []
            check("defaults reach the child: --sms-recheck 5",
                  rc == 0 and flag_value(batch, "--sms-recheck") == "5", batch)
            check("defaults reach the child: --sms-recheck-seconds 300.0",
                  flag_value(batch, "--sms-recheck-seconds") == "300.0", batch)
            check("a default run does not claim to skip the probe",
                  "--no-sms-check" not in batch, batch)

            out_b = os.path.join(td, "b", "results.jsonl")
            ex_b, _ = fake_execute({}, pathlib.Path(out_b))
            D._execute = capture(ex_b)
            rc = D.main(["--input", cards, "--out", out_b, "--skip-preflight",
                         "--sms-recheck", "2", "--sms-recheck-seconds", "45",
                         "--no-sms-check", "--yes"])
            batch = seen.get("batch") or []
            check("the operator can tune the watchdog from the day: 2 / 45.0",
                  rc == 0 and flag_value(batch, "--sms-recheck") == "2"
                  and flag_value(batch, "--sms-recheck-seconds") == "45.0", batch)
            check("--no-sms-check is passed down to the batch",
                  "--no-sms-check" in batch, batch)
            man = read_manifest(os.path.join(td, "b"))
            check("the manifest records the watchdog config",
                  man["config"]["sms_recheck"] == 2
                  and man["config"]["sms_recheck_seconds"] == 45.0
                  and man["config"]["no_sms_check"] is True, man["config"])

            out_c = os.path.join(td, "c", "results.jsonl")
            ex_c, _ = fake_execute({}, pathlib.Path(out_c))
            D._execute = capture(ex_c)
            rc = D.main(["--input", cards, "--out", out_c, "--preflight-only",
                         "--no-sms-check", "--yes"])
            pre = seen.get("preflight") or []
            check("--no-sms-check is passed down to the preflight too",
                  rc == 0 and "--no-sms-check" in pre, (rc, pre))

            out_d = os.path.join(td, "d", "results.jsonl")
            ex_d, _ = fake_execute({}, pathlib.Path(out_d))
            D._execute = capture(ex_d)
            rc = D.main(["--input", cards, "--out", out_d, "--skip-preflight",
                         "--sms-recheck", "0", "--sms-recheck-seconds", "0",
                         "--no-sms-check", "--yes"])
            batch = seen.get("batch") or []
            check("0 / 0 turns both triggers off",
                  rc == 0 and flag_value(batch, "--sms-recheck") == "0"
                  and flag_value(batch, "--sms-recheck-seconds") == "0.0", batch)
        finally:
            D._execute, D.Farm = old_exec, old_farm

    print("[17] the contract gate runs before anything is spent")
    with tempfile.TemporaryDirectory() as td:
        cards = write_cards(os.path.join(td, "cards.csv"), [("13800138031", "pw")])
        base_fp = os.path.join(td, "contract.json")
        with open(base_fp, "w", encoding="utf-8") as f:
            f.write("{}")
        old_exec, old_farm = D._execute, D.Farm
        D.Farm = StubFarm
        seen = {}

        def capture(script):
            def ex(cmd, log_path, echo=True):
                seen[step_name(cmd)] = list(cmd)
                return script(cmd, log_path, echo)
            return ex

        try:
            out_a = os.path.join(td, "a", "results.jsonl")
            ex_a, _ = fake_execute({}, pathlib.Path(out_a))
            D._execute = capture(ex_a)
            rc = D.main(["--input", cards, "--out", out_a, "--yes"])
            check("without --contract nothing extra runs", "contract" not in seen, list(seen))

            out_b = os.path.join(td, "b", "results.jsonl")
            ex_b, _ = fake_execute({}, pathlib.Path(out_b))
            D._execute = capture(ex_b)
            rc = D.main(["--input", cards, "--out", out_b, "--contract", base_fp,
                         "--base", "https://example.test", "--yes"])
            cmd = seen.get("contract") or []
            check("the gate runs and the day still finishes", rc == 0 and bool(cmd), (rc, list(seen)))
            check("it is the contract tool",
                  any(str(x).endswith("vmax_contract.py") for x in cmd), cmd)
            check("it checks against the baseline", flag_value(cmd, "--baseline") == base_fp, cmd)
            check("the base is handed down", flag_value(cmd, "--base") == "https://example.test", cmd)
            man = read_manifest(os.path.join(td, "b"))
            check("the manifest records the baseline",
                  man["config"]["contract"] == os.path.abspath(base_fp),
                  man["config"].get("contract"))

            out_c = os.path.join(td, "c", "results.jsonl")
            ex_c, calls_c = fake_execute({"contract": 1}, pathlib.Path(out_c))
            D._execute = capture(ex_c)
            rc = D.main(["--input", cards, "--out", out_c, "--contract", base_fp, "--yes"])
            man = read_manifest(os.path.join(td, "c"))
            check("a critical drift stops the day before the batch",
                  rc == 2 and "batch" not in calls_c, (rc, calls_c))
            check("and the verdict names it", man["verdict"] == "contract_drift", man.get("verdict"))

            out_d = os.path.join(td, "d", "results.jsonl")
            ex_d, calls_d = fake_execute({"contract": 2}, pathlib.Path(out_d))
            D._execute = capture(ex_d)
            rc = D.main(["--input", cards, "--out", out_d, "--contract", base_fp, "--yes"])
            man = read_manifest(os.path.join(td, "d"))
            check("cannot-judge gets its own verdict",
                  rc == 2 and man["verdict"] == "contract_unavailable", man.get("verdict"))

            out_e = os.path.join(td, "e", "results.jsonl")
            ex_e, calls_e = fake_execute({"contract": 1}, pathlib.Path(out_e))
            D._execute = capture(ex_e)
            rc = D.main(["--input", cards, "--out", out_e, "--contract", base_fp,
                         "--force", "--yes"])
            check("--force still runs the batch", rc == 0 and "batch" in calls_e, (rc, calls_e))
        finally:
            D._execute, D.Farm = old_exec, old_farm

    print("ALL DAY TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
