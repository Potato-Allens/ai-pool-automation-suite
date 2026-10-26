# -*- coding: utf-8 -*-
"""Red-team suite: one choice per card, one number per card, tamper + replay defence."""
import os, sys, threading, time, json, shutil, hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(BASE, "testdata_abuse")
if os.path.isdir(TESTDATA):
    shutil.rmtree(TESTDATA, ignore_errors=True)
os.makedirs(TESTDATA, exist_ok=True)
os.environ["SMSGATE_ROOT"] = BASE
os.environ["SMSGATE_DATA"] = TESTDATA
sys.path.insert(0, BASE)

MOCK_PORT = 18961
APP_PORT = 18960
B = "http://127.0.0.1:%d" % APP_PORT
STATE = {"calls": [], "lock": threading.Lock(), "seq": 0, "msg_delay": 0.25}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, text, status=200):
        body = str(text).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        with STATE["lock"]:
            STATE["calls"].append(q)
        if q.get("code") == "leftAmount":
            return self._send("68.06")
        if q.get("code") == "getPhone":
            with STATE["lock"]:
                STATE["seq"] += 1
                seq = STATE["seq"]
            return self._send("1370000%04d" % seq)
        if q.get("code") == "getMsg":
            time.sleep(STATE["msg_delay"])
            return self._send("【豆包】验证码 246810，5分钟内有效。")
        return self._send("ERROR:unknown")

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()

import uvicorn
from app.main import app
from app import db, nexq, providers

db.init()
for k, v in {
    "provider": "pool", "allow_countries": "us",
    "allow_choices": "us,cn_real,cn_virtual", "max_numbers_per_card": "1",
    "number_ttl_min": "20", "exit_grace_sec": "0",
    "d1jiema_token": "abuse-token", "d1jiema_enabled": "1",
    "d1jiema_base": "http://127.0.0.1:%d/zc/data.php" % MOCK_PORT,
    "d1jiema_keyword": "豆包", "d1jiema_sms_prefix": "[豆包]", "d1jiema_gap_ms": "120",
    "nexsms_queue_concurrency": "5", "nexsms_queue_max": "500",
    "nexsms_queue_timeout_sec": "120", "nexsms_cooldown_until": "0",
}.items():
    db.set_setting(k, v)

cfg = uvicorn.Config(app, host="127.0.0.1", port=APP_PORT, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.1)

import requests

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:300]) if extra else ""))


keys = {}
now = int(time.time())
with db.tx() as c:
    for tag in ("A", "B", "C", "D"):
        key = "ABUSE-%s-0001-TEST" % tag
        kh = hashlib.sha256(("sg:" + key).encode()).hexdigest()
        c.execute("INSERT INTO cards(key_hash,key_prefix,note,max_codes,status,batch_id,created_at,created_by) "
                  "VALUES(?,?,?,?, 'active', 0, ?, 'abuse')", (kh, key[:9], "abuse-" + tag, 1, now))
        keys[tag] = key


def open_card(tag, ip):
    h = {"X-Forwarded-For": ip}
    s = requests.Session()
    d = s.post(B + "/api/redeem", json={"key": keys[tag]}, headers=h, timeout=20).json()
    return s, d, h


def call(s, path, body, csrf, h, ip=None):
    hh = dict(h)
    if ip:
        hh["X-Forwarded-For"] = ip
    hh["X-CSRF"] = csrf
    return s.post(B + path, json=body, headers=hh, timeout=40)


def wait_number(s, h, timeout=30):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        r = s.get(B + "/api/session", headers=h, timeout=20).json()
        last = r
        if r.get("number"):
            return r
        time.sleep(0.25)
    return last or {}


# ---------------------------------------------------------------- F. tamper
sA, dA, hA = open_card("A", "10.9.0.1")
csrfA = dA.get("csrf", "")
check("A redeem", dA.get("ok"), dA)

sess = sA.get(B + "/api/session", headers=hA).json()
codes = [c["code"] for c in sess.get("choices", [])]
check("session lists choices", codes == ["us", "cn_real", "cn_virtual"], codes)
check("choices include country fields", all("stock" in c and "flag" in c for c in sess["choices"]))

r = call(sA, "/api/choice", {"choice": "cn_real", "count": 9, "csrf": csrfA}, csrfA, hA)
check("choice rejects extra params", r.status_code == 400, (r.status_code, r.text[:120]))
r = call(sA, "/api/number", {"choice": "cn_real", "provider": "d1jiema", "csrf": csrfA}, csrfA, hA)
check("number rejects extra params", r.status_code == 400, (r.status_code, r.text[:120]))
r = call(sA, "/api/number", {"choice": "xx", "csrf": csrfA}, csrfA, hA)
check("invalid choice rejected", r.status_code == 400, r.status_code)

# ---------------------------------------------------------------- A. one choice
r = call(sA, "/api/choice", {"choice": "cn_real", "csrf": csrfA}, csrfA, hA)
check("choice cn_real locked", r.status_code == 200 and r.json().get("locked"), r.text[:160])
r = call(sA, "/api/choice", {"choice": "us", "csrf": csrfA}, csrfA, hA)
check("second choice rejected", r.status_code == 409, (r.status_code, r.text[:160]))
r = call(sA, "/api/number", {"choice": "us", "csrf": csrfA}, csrfA, hA)
check("cannot switch choice at take-number", r.status_code == 409, (r.status_code, r.text[:160]))
r = call(sA, "/api/number", {"choice": "cn_real", "csrf": csrfA}, csrfA, hA)
check("cn_real allocated", r.status_code == 200 and
      str((r.json().get("number") or {}).get("phone", "")).startswith("137"), (r.status_code, r.text[:200]))

sess = wait_number(sA, hA)
numA = sess.get("number") or {}
check("number allocated", str(numA.get("phone", "")).startswith("137"), numA)
check("number choice cn_real", numA.get("choice") == "cn_real", numA)

r = call(sA, "/api/number", {"choice": "us", "csrf": csrfA}, csrfA, hA)
check("post-allocation switch blocked", r.status_code == 409, (r.status_code, r.text[:160]))
r = call(sA, "/api/number", {"choice": "cn_real", "csrf": csrfA}, csrfA, hA)
check("same choice returns same number", r.status_code == 200 and r.json().get("reused") is True, r.text[:200])
check("same phone", (r.json().get("number") or {}).get("phone") == numA.get("phone"), r.text[:200])

rows = db.q("SELECT * FROM numbers WHERE card_id=(SELECT id FROM cards WHERE key_prefix=?)", (keys["A"][:9],))
check("exactly one number row for A", len(rows) == 1, len(rows))
check("number carries choice", rows and rows[0]["choice"] == "cn_real", rows[0]["choice"] if rows else "")

r = call(sA, "/api/poll", {"csrf": csrfA}, csrfA, hA)
j = r.json()
check("poll got code", j.get("state") == "code" and j.get("code") == "246810", j)
check("force exit on redeem", j.get("force_exit") is True, j)
r = sA.get(B + "/api/session", headers=hA)
check("session revoked after code", r.status_code == 401, r.status_code)

# ---------------------------------------------------------------- B. 20 concurrent takes
sB, dB, hB = open_card("B", "10.9.1.1")
csrfB = dB.get("csrf", "")
r = call(sB, "/api/choice", {"choice": "cn_virtual", "csrf": csrfB}, csrfB, hB)
check("B locked cn_virtual", r.status_code == 200, r.text[:120])

before = len([x for x in STATE["calls"] if x.get("code") == "getPhone"])


def burst(i):
    return call(sB, "/api/number", {"choice": "cn_virtual", "csrf": csrfB}, csrfB, hB,
                ip="10.9.2.%d" % (i + 1)).status_code


with ThreadPoolExecutor(max_workers=20) as ex:
    statuses = list(ex.map(burst, range(20)))
check("burst no 5xx", all(x < 500 for x in statuses), statuses)
check("burst no queue (200/429 only)", all(x in (200, 429) for x in statuses) and 200 in statuses, statuses)

sessB = wait_number(sB, hB)
check("burst produced one number", bool((sessB.get("number") or {}).get("phone")), sessB)
time.sleep(1.0)
after = len([x for x in STATE["calls"] if x.get("code") == "getPhone"])
rowsB = db.q("SELECT COUNT(*) AS n FROM numbers WHERE card_id=(SELECT id FROM cards WHERE key_prefix=?)",
             (keys["B"][:9],))
tickB = db.q("SELECT COUNT(*) AS n FROM nex_queue WHERE card_id=(SELECT id FROM cards WHERE key_prefix=?)",
             (keys["B"][:9],))
check("burst bought exactly one number", rowsB[0]["n"] == 1, rowsB[0]["n"])
check("burst bought upstream once", after - before == 1, after - before)
check("no queue rows created", tickB[0]["n"] == 0, tickB[0]["n"])

# ---------------------------------------------------------------- C. concurrent polls
sC, dC, hC = open_card("C", "10.9.3.1")
csrfC = dC.get("csrf", "")
r = call(sC, "/api/choice", {"choice": "cn_real", "csrf": csrfC}, csrfC, hC)
check("C locked", r.status_code == 200, r.text[:120])
r = call(sC, "/api/number", {"choice": "cn_real", "csrf": csrfC}, csrfC, hC)
check("C allocated inline", r.status_code == 200 and
      bool((r.json().get("number") or {}).get("phone")), (r.status_code, r.text[:120]))
sessC = wait_number(sC, hC)
check("C has number", bool((sessC.get("number") or {}).get("phone")), sessC)


def poll_burst(i):
    return call(sC, "/api/poll", {"csrf": csrfC}, csrfC, hC, ip="10.9.4.%d" % (i + 1)).status_code


with ThreadPoolExecutor(max_workers=10) as ex:
    pst = list(ex.map(poll_burst, range(10)))
check("concurrent polls no 5xx", all(x < 500 for x in pst), pst)
for _ in range(3):
    call(sC, "/api/poll", {"csrf": csrfC}, csrfC, hC)
cardC = db.q1("SELECT * FROM cards WHERE key_prefix=?", (keys["C"][:9],))
check("used_codes incremented once", int(cardC["used_codes"]) == 1, cardC["used_codes"])
check("card marked used", cardC["status"] == "used", cardC["status"])

# ---------------------------------------------------------------- D. ticket replay
sD, dD, hD = open_card("D", "10.9.5.1")
csrfD = dD.get("csrf", "")
call(sD, "/api/choice", {"choice": "cn_real", "csrf": csrfD}, csrfD, hD)
r = call(sD, "/api/number", {"choice": "cn_real", "csrf": csrfD}, csrfD, hD)
check("D allocated inline", r.status_code == 200 and
      bool((r.json().get("number") or {}).get("phone")), (r.status_code, r.text[:150]))
r2 = call(sB, "/api/queue/status", {"ticket": 999999, "token": "x" * 12, "csrf": csrfB}, csrfB, hB)
check("stale ticket rejected", r2.status_code == 410, (r2.status_code, r2.text[:150]))
r3 = call(sD, "/api/queue/status", {"ticket": 999999, "token": "y" * 12, "csrf": csrfD}, csrfD, hD)
check("forged ticket token rejected", r3.status_code == 410, (r3.status_code, r3.text[:150]))

# ---------------------------------------------------------------- audit trail
v = db.q1("SELECT COUNT(*) AS n FROM events WHERE kind='choice_violation'")
t = db.q1("SELECT COUNT(*) AS n FROM events WHERE kind='request_tamper'")
check("choice violations audited", v["n"] >= 3, v["n"])
check("tamper attempts audited", t["n"] >= 2, t["n"])

print("\n=== %d/%d passed ===" % (sum(1 for _, ok in results if ok), len(results)))
sys.exit(0 if all(ok for _, ok in results) else 1)