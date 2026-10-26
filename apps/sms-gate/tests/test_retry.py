# -*- coding: utf-8 -*-
# Silent numbers must not burn the card: retry/switch + expiry cleanup.
import os, sys, threading, time, re, shutil, hashlib, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(BASE, "testdata_retry")
shutil.rmtree(TESTDATA, ignore_errors=True)
os.makedirs(TESTDATA, exist_ok=True)
os.environ["SMSGATE_ROOT"] = BASE
os.environ["SMSGATE_DATA"] = TESTDATA
os.environ["SMSGATE_PORT"] = "18989"
sys.path.insert(0, BASE)

import uvicorn, requests
from app.main import app
from app import db, security

MOCK_PORT = 18990


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("0|0".encode())


threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), H).serve_forever, daemon=True).start()
db.init()
db.set_setting("provider", "pool")
db.set_setting("allow_choices", "us")
db.set_setting("max_alloc_attempts", "3")
db.set_setting("number_ttl_min", "20")

now = int(time.time())
phones = ["+12025550201", "+12025550202", "+12025550203", "+12025550204", "+12025550205", "+12025550206",
          "+12025550207"]
with db.tx() as c:
    for p in phones:
        c.execute("INSERT INTO numbers(phone,country,provider,sms_url,status,created_at) "
                  "VALUES(?,?,?,?,'available',?)", (p, "us", "pool", "http://127.0.0.1:%d/sms" % MOCK_PORT, now))


def make_card(key, note=""):
    kh = hashlib.sha256(("sg:" + key).encode()).hexdigest()
    with db.tx() as c:
        c.execute("INSERT INTO cards(key_hash,key_prefix,note,max_codes,used_codes,status,created_at,choice) "
                  "VALUES(?,?,?,1,0,'active',?,'')", (kh, key[:4], note, int(time.time())))
        return c.execute("SELECT id FROM cards WHERE key_hash=?", (kh,)).fetchone()["id"]


cfg = uvicorn.Config(app, host="127.0.0.1", port=18989, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started: break
    time.sleep(0.1)

B = "http://127.0.0.1:18989"
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:280]) if extra else ""))


def session_for(key):
    s = requests.Session()
    r = s.post(B + "/api/redeem", json={"key": key})
    d = r.json()
    return s, d.get("csrf", ""), d


def num(s, csrf, retry=False):
    body = {"choice": "us", "csrf": csrf}
    if retry:
        body["retry"] = True
    return s.post(B + "/api/number", json=body, headers={"X-CSRF": csrf})


# ---------- card 1: manual retry ----------
cid1 = make_card("RETR-Y001-0001-0001", "retry")
s, csrf, d = session_for("RETR-Y001-0001-0001")
check("redeem ok", d.get("ok") and csrf, d.get("error"))

r = num(s, csrf)
d = r.json()
check("first number", r.status_code == 200 and d["number"]["phone"] == phones[0], d.get("error"))
check("attempts 1", d.get("attempts") == 1 and d.get("max_attempts") == 3, (d.get("attempts"), d.get("max_attempts")))
n1 = db.q1("SELECT * FROM numbers WHERE phone=? AND card_id=?", (phones[0], cid1))

r = num(s, csrf, retry=True)
d = r.json()
check("retry too early", r.status_code == 200 and d.get("retry_early") is True, d)

db.conn().execute("UPDATE numbers SET allocated_at=? WHERE id=?", (int(time.time()) - 60, n1["id"]))
r = num(s, csrf, retry=True)
d = r.json()
check("retry switches number", r.status_code == 200 and d["number"]["phone"] == phones[1], (r.status_code, d.get("error")))
check("attempts 2", d.get("attempts") == 2, d.get("attempts"))
check("old number expired", db.q1("SELECT status FROM numbers WHERE id=?", (n1["id"],))["status"] == "expired")
slot = db.q1("SELECT number_id FROM card_choices WHERE card_id=?", (cid1,))["number_id"]
n2 = db.q1("SELECT id FROM numbers WHERE phone=? AND card_id=?", (phones[1], cid1))["id"]
check("slot points to new number", slot == n2, (slot, n2))

db.conn().execute("UPDATE numbers SET allocated_at=? WHERE id=?", (int(time.time()) - 60, n2))
r = num(s, csrf, retry=True)
d = r.json()
check("third number", r.status_code == 200 and d["number"]["phone"] == phones[2], (r.status_code, d.get("error")))
check("attempts 3", d.get("attempts") == 3, d.get("attempts"))

db.conn().execute("UPDATE numbers SET allocated_at=? WHERE id=(SELECT id FROM numbers WHERE phone=? AND card_id=?)",
                  (int(time.time()) - 60, phones[2], cid1))
r = num(s, csrf, retry=True)
d = r.json()
check("fourth retry blocked", r.status_code == 409 and "用完" in (d.get("error") or ""), (r.status_code, d.get("error")))

# ---------- card 2: expiry path (no manual retry) ----------
cid2 = make_card("RETR-Y002-0002-0002", "expire")
s2, csrf2, d2 = session_for("RETR-Y002-0002-0002")
r = num(s2, csrf2)
d = r.json()
check("expiry first number", r.status_code == 200 and d["number"]["phone"] == phones[3], d.get("error"))
db.conn().execute("UPDATE numbers SET allocated_at=? WHERE phone=? AND card_id=?",
                  (int(time.time()) - 21 * 60, phones[3], cid2))
r = num(s2, csrf2)
d = r.json()
check("after expiry gets new number", r.status_code == 200 and d["number"]["phone"] == phones[4],
      (r.status_code, d.get("error")))
check("old marked expired", db.q1("SELECT status FROM numbers WHERE phone=? AND card_id=?",
                                 (phones[3], cid2))["status"] == "expired")

# ---------- janitor ----------
from app.main import _expire_stale_numbers
cid3 = make_card("RETR-Y003-0003-0003", "janitor")
s3, csrf3, _ = session_for("RETR-Y003-0003-0003")
r = num(s3, csrf3)
d = r.json()
check("janitor number", r.status_code == 200 and d["number"]["phone"] == phones[5], d.get("error"))
db.conn().execute("UPDATE numbers SET allocated_at=? WHERE phone=? AND card_id=?",
                  (int(time.time()) - 21 * 60, phones[5], cid3))
_expire_stale_numbers()
check("janitor expired number", db.q1("SELECT status FROM numbers WHERE phone=? AND card_id=?",
                                     (phones[5], cid3))["status"] == "expired")
check("janitor freed slot", db.q1("SELECT number_id FROM card_choices WHERE card_id=?", (cid3,))["number_id"] == 0)

# ---------- poll path: expired number frees the slot immediately ----------
cid4 = make_card("RETR-Y004-0004-0004", "pollexpire")
s4, csrf4, _ = session_for("RETR-Y004-0004-0004")
r = num(s4, csrf4)
d = r.json()
check("poll card number", r.status_code == 200 and d["number"]["phone"] == phones[6], d.get("error"))
db.conn().execute("UPDATE numbers SET allocated_at=? WHERE phone=? AND card_id=?",
                  (int(time.time()) - 21 * 60, phones[6], cid4))
r = s4.post(B + "/api/poll", json={"csrf": csrf4}, headers={"X-CSRF": csrf4})
check("poll on expired -> 410", r.status_code == 410, (r.status_code, r.text[:120]))
check("poll expiry freed slot", db.q1("SELECT number_id FROM card_choices WHERE card_id=?", (cid4,))["number_id"] == 0)

# ---------- audit ----------
n = db.q1("SELECT COUNT(*) AS n FROM events WHERE kind='number_retry'")["n"]
check("retry audited", n >= 2, n)

fails = [x for x in results if not x[1]]
print("\nSUMMARY %d/%d" % (len(results) - len(fails), len(results)))
if fails:
    print("FAILED:", [x[0] for x in fails]); sys.exit(1)