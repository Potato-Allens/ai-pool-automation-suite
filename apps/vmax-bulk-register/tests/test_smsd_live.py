# -*- coding: utf-8 -*-
"""Live checks for vmax_smsd + the http: SMS provider (real server, no phone).

Covers the whole "real SIM -> HTTP -> vmax_register" hop without a SIM:
  auth, ingest, long-poll, `since` filtering, code TTL, open-bind refusal,
  and the exact --sms URL documented in RUNBOOK/README.
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

import requests  # noqa: E402

from vmax_sms import build_sms, since_param  # noqa: E402

PY = sys.executable
SMSD = os.path.join(ROOT, "vmax_smsd.py")
SECRET = "<TEST_SECRET>-test"
PHONE = "13800138000"

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Daemon:
    def __init__(self, port: int, extra=None, secret: str = SECRET):
        self.port = port
        self.state = os.path.join(tempfile.mkdtemp(prefix="smsd_"), "codes.jsonl")
        cmd = [PY, SMSD, "--host", "127.0.0.1", "--port", str(port),
               "--state", self.state]
        if secret:
            cmd += ["--secret", secret]
        cmd += list(extra or [])
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def wait_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                r = requests.get(self.url("/health"), timeout=1)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.15)
        return False

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def ingest(d, phone=PHONE, text="", secret=SECRET, as_json=True):
    body = {"phone": phone, "text": text}
    headers = {"X-Sms-Secret": secret} if secret else {}
    url = d.url("/ingest")
    if as_json:
        return requests.post(url, json=body, headers=headers, timeout=10)
    return requests.post(url, data=body, headers=headers, timeout=10)


def main() -> int:
    port = free_port()
    d = Daemon(port)
    try:
        print("[1] daemon boots and answers /health")
        ok = d.wait_ready()
        check("ready", ok, "daemon did not start: "
              + (d.proc.stdout.read()[:400] if d.proc.poll() is not None else "timeout"))
        if not ok:
            return 1
        h = requests.get(d.url("/health"), timeout=5).json()
        check("health ok", h.get("ok") is True, h)

        print("[2] secrets are enforced on every code-bearing route")
        check("latest without secret -> 401",
              requests.get(d.url("/latest"), timeout=5).status_code == 401)
        check("latest with secret -> 200",
              requests.get(d.url("/latest?secret=" + SECRET), timeout=5).status_code == 200)
        check("ingest without secret -> 401", ingest(d, text="x", secret=None).status_code == 401)
        check("code without secret -> 401",
              requests.get(d.url(f"/code?phone={PHONE}"), timeout=5).status_code == 401)

        print("[3] ingest form + JSON both work, phone normalisation")
        r = ingest(d, text="\u3010VobileMax\u3011\u60a8\u7684\u9a8c\u8bc1\u7801\u4e3a 482913\uff0c5 \u5206\u949f\u5185\u6709\u6548\u3002")
        j = r.json()
        check("json ingest 200", r.status_code == 200, j)
        check("code parsed", j.get("code") == "482913", j)
        r2 = ingest(d, phone="+86 138-0013-8000", as_json=False,
                    text="\u9a8c\u8bc1\u7801\uff1a193827")
        check("form ingest 200", r2.status_code == 200, r2.text[:200])
        check("+86 normalised", r2.json().get("code") == "193827", r2.text[:200])
        check("bad phone -> 400", ingest(d, phone="12345", text="x").status_code == 400)

        print("[4] /code returns the newest code")
        r = requests.get(d.url(f"/code?phone={PHONE}&secret={SECRET}"), timeout=10)
        check("code 200", r.status_code == 200, r.text[:200])
        check("newest wins", r.json().get("code") == "193827", r.text[:200])

        print("[5] long-poll wakes up when the SMS lands")
        # a fresh number, otherwise the previous code is returned instantly
        phone2 = "13700137000"
        tel = threading.Thread(target=lambda: (time.sleep(1.2),
                                               ingest(d, phone=phone2,
                                                      text="\u60a8\u7684\u52a8\u6001\u7801 338291 \u5df2\u53d1\u9001")))
        t0 = time.time()
        tel.start()
        r = requests.get(d.url(f"/code?phone={phone2}&secret={SECRET}&timeout=15"), timeout=20)
        dt = time.time() - t0
        tel.join()
        check("long-poll 200", r.status_code == 200, r.text[:200])
        check("woke before timeout", 0.8 < dt < 6.0, round(dt, 2))
        check("fresh code", r.json().get("code") == "338291", r.text[:200])

        print("[6] since= rejects codes that arrived before a resend")
        t_now = time.time()
        r = requests.get(d.url(f"/code?phone={PHONE}&secret={SECRET}&since={int(t_now) + 1}&timeout=1"),
                         timeout=15)
        check("old code invisible after since", r.status_code == 404, r.text[:200])
        ingest(d, text="\u9a8c\u8bc1\u7801\uff1a770011")
        r = requests.get(d.url(f"/code?phone={PHONE}&secret={SECRET}&since={int(t_now)}&timeout=1"),
                         timeout=15)
        check("new code visible after since", r.status_code == 200 and r.json().get("code") == "770011",
              r.text[:200])

        print("[6b] since= keeps sub-second precision (stale code in the same second)")
        ph3 = "13500136009"
        j0 = ingest(d, phone=ph3, text="\u9a8c\u8bc1\u7801\uff1a111222").json()
        prev_ts = float(j0.get("ts") or 0)
        check("ingest ts is fractional (not int-truncated)", prev_ts != int(prev_ts), prev_ts)
        time.sleep(0.05)
        t_send = time.time()
        sp = since_param(t_send)
        check("since_param renders a fraction", sp.endswith("." + sp.split(".")[1]) and "." in sp, sp)
        r = requests.get(d.url(f"/code?phone={ph3}&secret={SECRET}&since={sp}&timeout=1"), timeout=10)
        check("previous code stays hidden while the next SMS is in flight",
              r.status_code == 404, r.text[:200])
        # Same property, without racing the wall clock: an int-truncated `since`
        # is <= the code's own (fractional) ts, so the old format hands the stale
        # code back *whenever* this runs.  The conditional version that compared
        # int(prev_ts) == int(t_send) skipped itself roughly half the runs, which
        # made the suite's assertion count (and every doc number) drift.
        r = requests.get(d.url(f"/code?phone={ph3}&secret={SECRET}&since={int(prev_ts)}&timeout=1"),
                         timeout=10)
        check("an int second would have handed back the stale code (old format)",
              r.status_code == 200 and r.json().get("code") == "111222", r.text[:200])
        ingest(d, phone=ph3, text="\u9a8c\u8bc1\u7801\uff1a333444")
        r = requests.get(d.url(f"/code?phone={ph3}&secret={SECRET}&since={sp}&timeout=3"), timeout=15)
        check("the new code still arrives", r.status_code == 200 and r.json().get("code") == "333444",
              r.text[:200])

        print("[7] ttl= hides expired codes (platform codes live ~5 min)")
        # A fresh code first: [6] ingested 770011 over a second ago, so asking for
        # it with ttl=1 would 404 for the wrong reason (a slow machine, not a bug).
        ingest(d, text="\u9a8c\u8bc1\u7801\uff1a661122")
        r = requests.get(d.url(f"/code?phone={PHONE}&secret={SECRET}&ttl=1"), timeout=5)
        check("ttl=1 right away still sees it", r.status_code == 200, r.text[:200])
        check("ttl=1 returns the code just ingested", r.json().get("code") == "661122", r.text[:200])
        time.sleep(1.3)
        r = requests.get(d.url(f"/code?phone={PHONE}&secret={SECRET}&ttl=1&timeout=1"), timeout=10)
        check("ttl=1 after 1.3s -> 404", r.status_code == 404, r.text[:200])
        check("default ttl still serves it",
              requests.get(d.url(f"/code?phone={PHONE}&secret={SECRET}"), timeout=5).status_code == 200)

        print("[8] the documented --sms URL drives HttpSms end to end")
        spec = (f"http:http://127.0.0.1:{d.port}/code?phone={{phone}}"
                f"&secret={SECRET}&timeout=20&since={{since}}")
        sms = build_sms(spec)
        t_send = time.time()
        threading.Thread(target=lambda: (time.sleep(0.8),
                                         ingest(d, text="G-880123 is your verification code"))).start()
        code = sms.wait(PHONE, timeout=15, since=t_send)
        check("HttpSms returned the code", code == "880123", code)
        sms_no_since = build_sms(f"http:http://127.0.0.1:{d.port}/code?phone={{phone}}&secret={SECRET}")
        check("spec without {{since}} still works", sms_no_since.wait(PHONE, timeout=5) == "880123")

        print("[8b] socket timeout follows the endpoint long-poll window")
        from vmax_sms import HttpSms
        check("timeout=180 -> 185s", HttpSms.request_timeout("http://h/code?phone=1&timeout=180") == 185.0)
        check("no window -> 15s", HttpSms.request_timeout("http://h/code?phone=1") == 15.0)
        check("absurd window is capped", HttpSms.request_timeout("http://h/code?timeout=9999") == 310.0)

        print("[9] unknown phone times out instead of inventing a code")
        t0 = time.time()
        r = requests.get(d.url(f"/code?phone=13600136000&secret={SECRET}&timeout=1"), timeout=10)
        check("404 timeout", r.status_code == 404 and r.json().get("error") == "timeout", r.text[:200])
        check("honoured 1s timeout", time.time() - t0 < 5, round(time.time() - t0, 2))
        print("[9b] concurrent long-polls for different phones (batch workers)")
        phones = {"13500136001": "111111", "13500136002": "222222", "13500136003": "333333"}
        results = {}

        def waiter(ph):
            r = requests.get(d.url(f"/code?phone={ph}&secret={SECRET}&timeout=12"), timeout=20)
            results[ph] = (r.status_code, (r.json() or {}).get("code") if r.content else None)

        ths = [threading.Thread(target=waiter, args=(ph,)) for ph in phones]
        for th in ths:
            th.start()
        time.sleep(0.8)
        for ph, code in phones.items():
            ingest(d, phone=ph, text=f"验证码：{code}")
        for th in ths:
            th.join(timeout=20)
        check("all three waiters answered", len(results) == 3, results)
        check("each got its own code", all(results.get(ph) == (200, code) for ph, code in phones.items()), results)

    finally:
        d.stop()

    print("[10] refuses an open bind (0.0.0.0 without --secret)")
    p2 = free_port()
    r = subprocess.run([PY, SMSD, "--host", "0.0.0.0", "--port", str(p2),
                        "--state", os.path.join(tempfile.mkdtemp(prefix="smsd_"), "c.jsonl")],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30)
    check("exit code 2", r.returncode == 2, r.returncode)
    check("explains why", "refusing" in (r.stdout + r.stderr).lower(), (r.stdout + r.stderr)[:200])

    print("[11] --self-test parsing corpus")
    r = subprocess.run([PY, SMSD, "--self-test"], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    check("self-test passes", r.returncode == 0 and "PASS" in r.stdout, r.stdout[-300:])

    print("ALL SMSD LIVE TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
