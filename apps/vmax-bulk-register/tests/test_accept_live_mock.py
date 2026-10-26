# -*- coding: utf-8 -*-
"""Prove the `--live` branch of vmax_accept without a phone or a browser.

Chain under test:
    mock auth server  ← send-code / register / oauth   (tests/mock_e2e.py)
    vmax_smsd         ← /ingest  (a thread plays the phone forwarder)
    vmax_accept --live --yes  →  one "account"  →  accept.json + accept_live.jsonl

Everything except the SIM is the production wiring: real HTTP, real daemon,
real subprocess call, real JSONL parsing.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import requests  # noqa: E402

import mock_e2e as M  # noqa: E402

import vmax_accept as A  # noqa: E402

PY = sys.executable
SMSD = os.path.join(ROOT, "vmax_smsd.py")
SECRET = "<TEST_SECRET>-live"
PHONE = "13500135000"

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def forwarder(sms_dir: str, daemon: str, stop: threading.Event):
    """Stand-in for SMS Forwarder: watch <phone>.txt and POST it to the inbox."""
    seen = {}
    while not stop.is_set():
        try:
            for name in os.listdir(sms_dir):
                if not name.endswith(".txt"):
                    continue
                path = os.path.join(sms_dir, name)
                mtime = os.path.getmtime(path)
                if seen.get(path) == mtime:
                    continue
                seen[path] = mtime
                phone = name[:-4]
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                requests.post(daemon + "/ingest", json={"phone": phone, "text": text},
                              headers={"X-Sms-Secret": SECRET}, timeout=5)
        except Exception:
            pass
        stop.wait(0.3)


def report_tests(tmp: str, live_report: str) -> None:
    """[4] the --report view: pure rendering + CLI exit codes, no network."""
    print("[4] vmax_accept --report renders accept.json for humans")

    check("status ok", A.check_status({"ok": True}) == "ok")
    check("status skip (soft pass)", A.check_status({"ok": True, "hard": False}) == "skip")
    check("status fail (hard)", A.check_status({"ok": False}) == "fail")
    check("status warn (soft fail)", A.check_status({"ok": False, "hard": False}) == "warn")

    reuse = A.form_cost({"form_captcha": "reuse"})
    dev = A.form_cost({"form_captcha": "device", "device": {"ok": True}})
    check("cost: reuse rides free", reuse["extra_tokens"] == 0, reuse)
    check("cost: device rides free and reports the trust",
          dev["extra_tokens"] == 0 and dev["device_ok"] is True, dev)
    check("cost: fresh buys one", A.form_cost({"form_captcha": "fresh"})["extra_tokens"] == 1)
    check("cost: device_retry buys one",
          A.form_cost({"form_captcha": "device_retry"})["extra_tokens"] == 1)
    check("cost: an unknown/legacy row is None, never 0",
          A.form_cost({})["extra_tokens"] is None and A.form_cost(None)["path"] == "none")
    check("policy: a refused device is a paid ride",
          A.form_cost_ok("device", A.form_cost({"form_captcha": "device_fallback"})) is False)
    check("policy: fresh pays by design",
          A.form_cost_ok("fresh", A.form_cost({"form_captcha": "fresh"})) is True)

    good = {"ts": time.time(), "base": "https://auth.vobilemax.cn", "ready": True,
            "hard_failures": [],
            "checks": [{"check": "health", "ok": True, "detail": "200", "hard": True},
                       {"check": "token_queue", "ok": False, "detail": "0 tokens", "hard": False}]}
    lines = A.render_report(good, source="accept.json")
    text = "\n".join(lines)
    check("title", lines[0].startswith("ACCEPT REPORT"), lines[0])
    check("ready line", "ready  True" in text, text[:200])
    check("no NOT READY", "NOT READY" not in text)
    check("summary counts", "ok=1" in text and "warn=1" in text and "fail=0" in text)
    check("soft fail is not ALL GOOD", "ALL GOOD" not in text)

    bad = {"ts": time.time(), "base": "x", "ready": False,
           "hard_failures": ["smsd_health"],
           "checks": [{"check": "smsd_health", "ok": False,
                       "detail": "ConnectionError: " + "x" * 300, "hard": True}]}
    lines = A.render_report(bad, source="bad.json", detail_width=60)
    text = "\n".join(lines)
    check("not ready banner", "NOT READY" in text, text[:200])
    check("hard list echoed", "smsd_health" in text)
    check("stale note", "older than 1h" not in text)  # fresh ts
    check("detail truncated", "..." in text and max(len(l) for l in lines) < 160,
          max(len(l) for l in lines))
    check("empty checks tolerated",
          "no checks recorded" in "\n".join(A.render_report({"ready": True, "checks": []})))

    p_good = os.path.join(tmp, "rep_good.json")
    p_bad = os.path.join(tmp, "rep_bad.json")
    with open(p_good, "w", encoding="utf-8") as f:
        json.dump(good, f)
    with open(p_bad, "w", encoding="utf-8") as f:
        json.dump(bad, f)

    def run_report(path):
        return subprocess.run([PY, "vmax_accept.py", "--report", path], cwd=ROOT,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    r = run_report(p_good)
    check("cli ready exit 0", r.returncode == 0, r.returncode)
    check("cli prints table", "ACCEPT REPORT" in r.stdout and "summary" in r.stdout)
    r = run_report(p_bad)
    check("cli not-ready exit 1", r.returncode == 1, r.returncode)
    r = run_report(os.path.join(tmp, "missing.json"))
    check("cli missing exit 2", r.returncode == 2, r.returncode)
    check("cli missing message", "run the gate first" in r.stdout)

    p_bom = os.path.join(tmp, "rep_bom.json")
    with open(p_bom, "wb") as f:
        f.write(b"\xef\xbb\xbf" + json.dumps(good).encode("utf-8"))
    r = run_report(p_bom)
    check("cli tolerates BOM", r.returncode == 0, r.stdout[-200:])

    if os.path.exists(live_report):
        r = run_report(live_report)
        check("cli on real accept_live_test.json", r.returncode == 0, r.stdout[-200:])


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="accept_live_")
    sms_dir = os.path.join(tmp, "sms")
    os.makedirs(sms_dir, exist_ok=True)

    M.Cfg.cooldown = 0
    M.Cfg.reject_first = 0
    M.Cfg.omit_code = False
    M.Cfg.codes.clear()
    M.Cfg.attempts.clear()
    M.Cfg.last_send.clear()
    M.Cfg.sms_dir = sms_dir
    M.Cfg.codes_path = os.path.join(tmp, "codes.jsonl")

    dport = free_port()
    daemon = f"http://127.0.0.1:{dport}"
    d = subprocess.Popen([PY, SMSD, "--host", "127.0.0.1", "--port", str(dport),
                          "--secret", SECRET, "--state", os.path.join(tmp, "inbox.jsonl")],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = threading.Event()
    th = None
    try:
        for _ in range(60):
            try:
                requests.get(daemon + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.15)
        else:
            check("daemon ready", False)
            return 1

        with M.Server() as srv:
            base = f"http://127.0.0.1:{srv.port}"
            th = threading.Thread(target=forwarder, args=(sms_dir, daemon, stop), daemon=True)
            th.start()

            print("[1] vmax_accept --live drives one real registration")
            out = os.path.join(ROOT, "accept_live_test.json")
            if os.path.exists(out):
                os.remove(out)
            r = subprocess.run([
                PY, "vmax_accept.py", "--skip-preflight",
                "--smsd", daemon, "--secret", SECRET,
                "--live", "--yes", "--phone", PHONE,
                "--base", base, "--redirect", base + "/auth/callback",
                "--captcha", "param", "--captcha-param", M.Cfg.token,
                "--form-captcha", "reuse", "--sms-resends", "0",
                "--sms", f"http:{daemon}/code?phone={{phone}}&secret={SECRET}&timeout=10&since={{since}}",
                "--out", out,
            ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
            print(r.stdout.strip().splitlines()[-6:] and "\n".join(r.stdout.strip().splitlines()[-6:]))
            check("accept exit 0", r.returncode == 0, r.returncode)
            rep = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
            check("report ready", rep.get("ready") is True, rep.get("hard_failures"))
            live = rep.get("live") or {}
            check("live ok", live.get("ok") is True, live)
            check("uid present", bool(live.get("uid")), live)
            check("one register write", M.Cfg.register_tries == 1, M.Cfg.register_tries)
            mods = [c for c in (rep.get("checks") or []) if c.get("check") == "smsd_modem"]
            check("no --modem -> the modem row is a soft pass",
                  bool(mods) and mods[0]["ok"] is True and mods[0]["hard"] is False, mods)
            check("live records the form path", live.get("form_captcha") == "reuse", live)
            check("live records 0 extra form tokens",
                  live.get("form_extra_tokens") == 0, live)
            cost_rows = [c for c in (rep.get("checks") or [])
                         if c.get("check") == "live_form_cost"]
            check("exactly one live_form_cost row", len(cost_rows) == 1, cost_rows)
            check("the reuse spend matches the policy (soft pass)",
                  bool(cost_rows) and cost_rows[0]["ok"] is True
                  and cost_rows[0]["hard"] is False, cost_rows)

            print("[2] accept_live.jsonl keeps the account record")
            jl = os.path.join(ROOT, "accept_live.jsonl")
            recs = [json.loads(l) for l in open(jl, encoding="utf-8") if l.strip()]
            rec = recs[-1] if recs else {}
            check("status ok", rec.get("status") == "ok", rec.get("status"))
            check("phone echoed", rec.get("phone") == PHONE, rec.get("phone"))
            tt = str((rec.get("token") or {}).get("token_type") or "")
            check("token type bearer", tt.lower() == "bearer", tt)

            print("[3] guard: --live without --yes is refused")
            r = subprocess.run([PY, "vmax_accept.py", "--skip-preflight", "--live",
                                "--phone", PHONE, "--out", os.path.join(tmp, "x.json")],
                               cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            check("exit code 2", r.returncode == 2, r.returncode)

            print("[5] a daemon whose modem is down is NOT ACCEPT_READY")
            # The failure mode this catches: process up, module unplugged / SIM
            # PIN-locked.  /health still says ok=true, so without modems[] the
            # gate would happily print ACCEPT_READY and the first real card would
            # die at send-code.
            import http.server

            class Stub(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def _j(self, code, obj):
                    b = json.dumps(obj).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)

                def do_GET(self):
                    if self.path.startswith("/health"):
                        return self._j(200, {"ok": True, "stored": 0,
                                             "modems": [{"name": "COM3", "connected": False,
                                                         "last_error": "no SIM"}]})
                    if self.path.startswith("/code"):
                        return self._j(404, {"ok": False, "error": "timeout"})
                    if "secret=" + SECRET in self.path:
                        return self._j(200, {"ok": True, "items": []})
                    return self._j(401, {"ok": False, "error": "bad secret"})

            stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
            threading.Thread(target=stub.serve_forever, daemon=True).start()
            try:
                out5 = os.path.join(tmp, "accept_modem_down.json")
                r5 = subprocess.run([PY, "vmax_accept.py", "--skip-preflight",
                                     "--smsd", "http://127.0.0.1:%d" % stub.server_address[1],
                                     "--secret", SECRET, "--out", out5],
                                    cwd=ROOT, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
                rep5 = json.load(open(out5, encoding="utf-8"))
                rows5 = {c["check"]: c for c in rep5["checks"]}
                check("5 exit 1, not ready", r5.returncode == 1, r5.returncode)
                check("5 smsd_modem is the only hard failure",
                      rep5["hard_failures"] == ["smsd_modem"], rep5["hard_failures"])
                check("5 the reason names the port and the error",
                      "COM3:down" in rows5["smsd_modem"]["detail"]
                      and "no SIM" in rows5["smsd_modem"]["detail"],
                      rows5["smsd_modem"]["detail"])
            finally:
                stub.shutdown()
                stub.server_close()

            print("[6] a daemon with a virtual modem reports the port as up")
            vport = free_port()
            vd = subprocess.Popen([PY, SMSD, "--host", "127.0.0.1", "--port", str(vport),
                                   "--secret", SECRET, "--modem", "virtual",
                                   "--state", os.path.join(tmp, "inbox_virtual.jsonl")],
                                  cwd=ROOT, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            try:
                for _ in range(80):
                    try:
                        requests.get("http://127.0.0.1:%d/health" % vport, timeout=1)
                        break
                    except Exception:
                        time.sleep(0.1)
                out6 = os.path.join(tmp, "accept_virtual.json")
                r6 = subprocess.run([PY, "vmax_accept.py", "--skip-preflight",
                                     "--smsd", "http://127.0.0.1:%d" % vport,
                                     "--secret", SECRET, "--out", out6],
                                    cwd=ROOT, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
                rep6 = json.load(open(out6, encoding="utf-8"))
                rows6 = {c["check"]: c for c in rep6["checks"]}
                check("6 ready", r6.returncode == 0 and rep6["ready"] is True,
                      (r6.returncode, rep6["hard_failures"]))
                check("6 modem row is up", rows6["smsd_modem"]["ok"] is True,
                      rows6["smsd_modem"])
                check("6 the port name is in the detail",
                      "virtual:up" in rows6["smsd_modem"]["detail"], rows6["smsd_modem"])
            finally:
                vd.terminate()
                try:
                    vd.wait(timeout=5)
                except Exception:
                    vd.kill()
    finally:
        stop.set()
        if th:
            th.join(timeout=2)
        d.terminate()
        try:
            d.wait(timeout=5)
        except Exception:
            d.kill()

    report_tests(tmp, os.path.join(ROOT, "accept_live_test.json"))

    print("ALL ACCEPT-LIVE TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
