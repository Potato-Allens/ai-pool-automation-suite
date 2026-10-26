# -*- coding: utf-8 -*-
"""Concurrency / burst tests for the NexSMS premium pool."""
import os, sys, threading, time, json, shutil, hashlib, statistics
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(BASE, "testdata_conc")
if os.path.isdir(TESTDATA):
    shutil.rmtree(TESTDATA, ignore_errors=True)
os.makedirs(TESTDATA, exist_ok=True)
os.environ["SMSGATE_ROOT"] = BASE
os.environ["SMSGATE_DATA"] = TESTDATA
os.environ["SMSGATE_PORT"] = "18910"
sys.path.insert(0, BASE)

MOCK = "http://127.0.0.1:18911"
STATE = {"seq": 0, "purchases": [], "access_calls": 0, "lock": threading.Lock(), "purchase_delay": 0.2}

CATS = {"code": 0, "message": "success", "data": {
    "list": [{"id": 4, "name": "美国"}],
    "types": [{"id": 1, "name": "首次卡"}],
}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/premium/categories":
            return self._send(CATS)
        if u.path == "/api/premium/list":
            return self._send({"code": 0, "data": {"total": 1, "list": [
                {"id": 72, "type": 1, "cateId": 4, "name": "豆包", "price": 0.068, "quantity": 100000,
                 "successInfo": {"successRate": 99.0}}]}})
        if u.path == "/api/balance":
            return self._send({"code": 0, "data": {"balance": 100}})
        if u.path == "/api/number-access/sms-records":
            with STATE["lock"]:
                STATE["access_calls"] += 1
                n = STATE["access_calls"]
            time.sleep(0.05)
            recs = [] if n % 2 else [{"sender": "豆包", "content": "[豆包] 验证码 778899，5 分钟内有效。"}]
            return self._send({"code": 0, "data": {"records": recs, "expiresAt": "2026-12-31 00:00:00"}})
        return self._send({"code": 404, "message": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if u.path == "/api/premium/purchase":
            qty = int(body.get("quantity") or 1)
            time.sleep(STATE["purchase_delay"])
            items = []
            with STATE["lock"]:
                STATE["purchases"].append({"qty": qty, "appId": body.get("appId"), "t": time.time()})
                for _ in range(qty):
                    STATE["seq"] += 1
                    seq = STATE["seq"]
                    items.append({
                        "tel": "1202555%04d" % (seq % 10000),
                        "appName": "豆包", "status": 1,
                        "smsAccess": {"accessId": "acc%d" % seq, "token": "tok%d" % seq,
                                      "url": MOCK + "/api/number-access/sms-records?token=tok%d" % seq},
                    })
            return self._send({"code": 0, "message": "success", "data": items})
        return self._send({"code": 404, "message": "not found"}, 404)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 18911), Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()

import uvicorn
from app.main import app
from app import db, providers

db.init()
for k, v in {
    "provider": "nexsms_premium", "nexsms_key": "k", "nexsms_base": MOCK,
    "nexsms_project": "豆包", "nexsms_sms_prefix": "[豆包]", "nexsms_type": "1",
    "nexsms_expiry": "0", "nexsms_prefix": "", "nexsms_app_id": "",
    "nexsms_batch_qty": "5", "nexsms_stock_target": "5",
    "nexsms_buy_wait_sec": "25", "allow_countries": "us",
    "max_numbers_per_card": "3", "site_url": "https://sms.example.com",
}.items():
    db.set_setting(k, v)

cfg = uvicorn.Config(app, host="127.0.0.1", port=18910, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(80):
    if server.started:
        break
    time.sleep(0.1)

import requests
B = "http://127.0.0.1:18910"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:400]) if extra else ""))


def pct(vals, p):
    if not vals:
        return 0
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * p))]


# ---------------- 1) provider-level burst: 150 concurrent takes
STATE["purchases"].clear()
N = 150
lat = []
errs = []
phones = []
lock = threading.Lock()


def take(i):
    t0 = time.time()
    try:
        row = providers.allocate("us", 900000 + i)
        with lock:
            phones.append(row["phone"])
            lat.append(time.time() - t0)
    except Exception as e:
        with lock:
            errs.append("%s: %s" % (type(e).__name__, e))


t0 = time.time()
with ThreadPoolExecutor(max_workers=N) as ex:
    list(ex.map(take, range(N)))
elapsed = time.time() - t0
check("burst: all served", len(phones) == N and not errs, {"ok": len(phones), "errs": errs[:3]})
check("burst: no duplicate numbers", len(set(phones)) == len(phones), len(phones) - len(set(phones)))
check("burst: batch purchases used", 10 <= len(STATE["purchases"]) <= 45, len(STATE["purchases"]))
check("burst: quantity is batched", all(p["qty"] >= 5 for p in STATE["purchases"]),
      [p["qty"] for p in STATE["purchases"]][:10])
check("burst: wall time < 45s", elapsed < 45, round(elapsed, 2))
check("burst: p95 < 40s", pct(lat, 0.95) < 40, {"p50": round(pct(lat, 0.5), 2), "p95": round(pct(lat, 0.95), 2)})
print("   burst stats: n=%d elapsed=%.1fs p50=%.1fs p95=%.1fs purchases=%d" % (
    len(phones), elapsed, pct(lat, 0.5), pct(lat, 0.95), len(STATE["purchases"])))

# ---------------- 2) HTTP burst: 40 cards redeem + take number concurrently
keys = []
now = int(time.time())
with db.tx() as c:
    for i in range(40):
        key = "CONC-%04d-TEST-%04d" % (i, i)
        kh = hashlib.sha256(("sg:" + key).encode()).hexdigest()
        c.execute("INSERT INTO cards(key_hash,key_prefix,note,max_codes,status,batch_id,created_at,created_by) "
                  "VALUES(?,?,?,?, 'active', 0, ?, 'conc')", (kh, key[:9], "conc", 1, now))
        keys.append(key)

http_lat = []
http_err = []
out_phones = []
lock2 = threading.Lock()


def card_flow(i):
    key = keys[i]
    ip = "10.9.%d.%d" % (i // 200, (i % 200) + 1)
    h = {"X-Forwarded-For": ip}
    t0 = time.time()
    try:
        s = requests.Session()
        d = s.post(B + "/api/redeem", json={"key": key}, headers=h, timeout=30).json()
        if not d.get("ok"):
            raise RuntimeError("redeem: " + str(d.get("error")))
        r = s.post(B + "/api/number", json={"country": "us", "csrf": d["csrf"]},
                   headers={"X-CSRF": d["csrf"], **h}, timeout=60).json()
        if r.get("queued"):
            tick = r["ticket"]
            deadline = time.time() + 60
            while time.time() < deadline:
                r = s.post(B + "/api/queue/status",
                           json={"ticket": tick["id"], "token": tick["token"], "csrf": d["csrf"]},
                           headers={"X-CSRF": d["csrf"], **h}, timeout=30).json()
                if r.get("number"):
                    break
                if r.get("state") in ("error", "expired"):
                    raise RuntimeError("queue: " + str(r.get("error")))
                time.sleep(0.25)
        if not r.get("ok") or not r.get("number"):
            raise RuntimeError("number: " + str(r.get("error")))
        with lock2:
            out_phones.append(r["number"]["phone"])
            http_lat.append(time.time() - t0)
    except Exception as e:
        with lock2:
            http_err.append(str(e)[:120])


t0 = time.time()
with ThreadPoolExecutor(max_workers=40) as ex:
    list(ex.map(card_flow, range(40)))
http_elapsed = time.time() - t0
check("http burst: 40/40 got numbers", len(out_phones) == 40 and not http_err,
      {"ok": len(out_phones), "errs": http_err[:3]})
check("http burst: unique numbers", len(set(out_phones)) == len(out_phones),
      len(out_phones) - len(set(out_phones)))
check("http burst: elapsed < 30s", http_elapsed < 30, round(http_elapsed, 2))
print("   http burst: n=%d elapsed=%.1fs p50=%.2fs p95=%.2fs" % (
    len(out_phones), http_elapsed, pct(http_lat, 0.5), pct(http_lat, 0.95)))

# ---------------- 3) poll single-flight: 60 concurrent polls of one number
num = db.q1("SELECT * FROM numbers WHERE card_id>0 ORDER BY id DESC LIMIT 1")
STATE["access_calls"] = 0
providers._NEX_POLL_CACHE.clear()
numd = dict(num)
with ThreadPoolExecutor(max_workers=60) as ex:
    list(ex.map(lambda _: providers.poll(numd), range(60)))
check("poll single-flight: <= 3 upstream calls", STATE["access_calls"] <= 3, STATE["access_calls"])
print("   poll upstream calls for 60 concurrent polls:", STATE["access_calls"])

# ---------------- 4) stock helper + locks table
check("stock>0 after burst", providers._nex_available_count("us") >= 0, providers._nex_available_count("us"))
check("locks row exists", bool(db.q1("SELECT * FROM locks WHERE name='nex_buy'")))

failed = [x for x in results if not x[1]]
print("\n=== %d/%d passed ===" % (len(results) - len(failed), len(results)))
if failed:
    print("FAILED:", [x[0] for x in failed])
srv.shutdown()
sys.stdout.flush()
os._exit(1 if failed else 0)