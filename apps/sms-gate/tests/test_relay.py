# -*- coding: utf-8 -*-
# Relay cards: 卡号:+phone----upstream  ->  own-domain reusable /smsrecord?token=...
import os, sys, threading, time, re, shutil, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(BASE, "testdata_relay")
if os.path.isdir(TESTDATA):
    shutil.rmtree(TESTDATA, ignore_errors=True)
os.makedirs(TESTDATA, exist_ok=True)
os.environ["SMSGATE_ROOT"] = BASE
os.environ["SMSGATE_DATA"] = TESTDATA
os.environ["SMSGATE_PORT"] = "18995"
os.environ["SMSGATE_RELAY_ALLOW_PRIVATE"] = "1"
sys.path.insert(0, BASE)

import uvicorn, requests
from app.main import app
from app import db, security, relay_api

MOCK_PORT = 18996
HITS = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(self.path)
        if self.path.startswith("/sms?token=ok"):
            body = "1|【豆包】验证码 123456，用于手机验证码登录，5分钟内有效"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
        elif self.path.startswith("/sms?token=empty"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("0|0".encode())
        elif self.path.startswith("/sms?token=boom"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"upstream stack trace /secret")
        elif self.path.startswith("/clock"):
            body = "no|该号将于2026-12-08 00:00:00到期，请立即换绑。 暂时未有邮件...试试重发吧."
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()


threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), H).serve_forever, daemon=True).start()

db.init()
db.set_setting("relay_base", "https://api.sms.example.com")
db.set_setting("relay_rate", "60")
db.set_setting("relay_ip_rate", "2000")

cfg = uvicorn.Config(app, host="127.0.0.1", port=18995, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.1)

B = "http://127.0.0.1:18995"
M = "http://127.0.0.1:%d" % MOCK_PORT
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:260]) if extra else ""))


# ---------- admin session ----------
adm = requests.Session()
token = open(os.path.join(TESTDATA, "setup.token"), encoding="utf-8").read().strip()
d = adm.post(B + "/admin/api/setup", json={"token": token, "username": "admin", "password": "StrongPass123"}).json()
sec = re.search(r"secret=([A-Z2-7]+)", d["otpauth"]).group(1)
r = adm.post(B + "/admin/api/setup/verify", json={"code": security.totp_at(sec)}, headers={"X-CSRF": d["csrf"]})
csrf = d["csrf"]
check("admin setup", r.status_code == 200, r.text[:120])


def create(lines, note="t", days=0):
    r = adm.post(B + "/admin/api/relay/create", json={"lines": lines, "note": note, "expires_days": days},
                 headers={"X-CSRF": csrf})
    return r.status_code, r.json()


# ---------- parsing ----------
it = relay_api.parse_line("卡号:+12025550123----https://upstream.example.com/smsrecord?token=abc")
check("parse 卡号 colon", it and it["phone"] == "+12025550123" and it["upstream"].endswith("token=abc"), it)
it = relay_api.parse_line("卡号：+44 7700 900123----[https://x.test/sms?token=y](https://x.test/sms?token=y)")
check("parse fullwidth + markdown", it and it["phone"] == "+447700900123" and "x.test/sms" in it["upstream"], it)
it = relay_api.parse_line("+12025550123|https://x.test/s?t=1")
check("parse pipe", it and it["upstream"].startswith("https://x.test"), it)
it = relay_api.parse_line("+12025550123 https://x.test/s?t=2")
check("parse space", it and it["upstream"].endswith("t=2"), it)
check("parse junk rejected", relay_api.parse_line("1234") is None)

# ---------- create ----------
lines = ("卡号:+12025550123----%s/sms?token=ok\n"
         "卡号：+447700900123----[%s/clock](%s/clock)\n"
         "garbage line\n" % (M, M, M))
code, d = create(lines, note="relay-test")
check("create status 200", code == 200 and d.get("ok"), d)
check("create parsed 2", d.get("count") == 2, d.get("count"))
check("create skips garbage", len(d.get("bad") or []) == 1, d.get("bad"))
gen = d.get("lines") or []
check("line format", all(x.startswith("卡号:+") and "----https://api.sms.example.com/smsrecord?token=" in x for x in gen), gen)
check("line hides upstream", all("127.0.0.1" not in x for x in gen), gen)
tok1 = gen[0].split("token=")[1]
tok2 = gen[1].split("token=")[1]

# ---------- public relay ----------
r = requests.get(B + "/smsrecord", params={"token": tok1})
check("relay raw 200", r.status_code == 200, r.text)
check("relay raw body", "123456" in r.text and "豆包" in r.text, r.text)
check("relay CORS", r.headers.get("Access-Control-Allow-Origin") == "*", dict(r.headers))
check("relay no-store", "no-store" in (r.headers.get("Cache-Control") or ""), r.headers.get("Cache-Control"))
check("relay hides upstream", "127.0.0.1" not in r.text)

r = requests.get(B + "/smsrecord", params={"token": tok1, "fmt": "json"})
j = r.json()
check("relay json code", j.get("ok") and j.get("code") == "123456" and j.get("phone") == "+12025550123", j)

r = requests.get(B + "/smsrecord", params={"token": tok1, "code": "1"})
check("relay code-only", r.text.strip() == "123456", r.text)

r = requests.get(B + "/smsrecord", params={"token": tok2})
check("clock text passthrough", "暂时未有邮件" in r.text and "127.0.0.1" not in r.text, r.text[:80])

# reusable: read same token 3 times
okc = 0
for _ in range(3):
    rr = requests.get(B + "/smsrecord", params={"token": tok1})
    okc += 1 if rr.status_code == 200 else 0
check("reusable 3 reads", okc == 3, okc)

r = requests.get(B + "/smsrecord", params={"token": tok1}, headers={"Origin": "https://buyer.test"})
check("CORS on GET", r.headers.get("Access-Control-Allow-Origin") == "*")
r = requests.options(B + "/smsrecord")
check("CORS preflight", r.status_code == 204 and r.headers.get("Access-Control-Allow-Origin") == "*", r.status_code)

# ---------- admin list / reads ----------
r = adm.get(B + "/admin/api/relay", params={"q": "+12025550123"})
lst = r.json()
row = (lst.get("items") or [{}])[0]
check("list reads counted", row.get("reads", 0) >= 4, row.get("reads"))
check("list hides full upstream", row.get("upstream_host") == "127.0.0.1", row.get("upstream_host"))
rid = row.get("id")

# ---------- bad token / disabled / expired ----------
check("bad token 404", requests.get(B + "/smsrecord", params={"token": "deadbeef" * 4}).status_code == 404)
adm.post(B + "/admin/api/relay/%d/action" % rid, json={"action": "disable"}, headers={"X-CSRF": csrf})
check("disabled 403", requests.get(B + "/smsrecord", params={"token": tok1}).status_code == 403)
adm.post(B + "/admin/api/relay/%d/action" % rid, json={"action": "enable"}, headers={"X-CSRF": csrf})
check("enabled 200", requests.get(B + "/smsrecord", params={"token": tok1}).status_code == 200)
code, d = create("+12025550199----%s/sms?token=ok" % M, days=1)
short = d["lines"][0].split("token=")[1]
db.conn().execute("UPDATE relay_cards SET expires_at=? WHERE token=?", (int(time.time()) - 5, short))
check("expired 410", requests.get(B + "/smsrecord", params={"token": short}).status_code == 410)

# ---------- upstream error is generic ----------
code, d = create("+12025550198----%s/sms?token=boom" % M)
boom = d["lines"][0].split("token=")[1]
r = requests.get(B + "/smsrecord", params={"token": boom})
check("upstream 500 -> 502", r.status_code == 502, r.status_code)
check("upstream error hides detail", "stack trace" not in r.text and "127.0.0.1" not in r.text, r.text)

# ---------- rate limit ----------
db.set_setting("relay_rate", "2")
code, d = create("+12025550197----%s/sms?token=empty" % M)
rl = d["lines"][0].split("token=")[1]
codes = [requests.get(B + "/smsrecord", params={"token": rl}).status_code for _ in range(4)]
check("token rate limit 429", codes.count(429) >= 1 and codes[0] == 200, codes)
db.set_setting("relay_rate", "60")

# ---------- SSRF guard ----------
os.environ.pop("SMSGATE_RELAY_ALLOW_PRIVATE", None)
code, d = create("+12025550196----http://127.0.0.1:%d/sms?token=ok" % MOCK_PORT)
check("SSRF private rejected", code == 400 and d.get("ok") is False and d.get("bad"), d)
code, d = create("+12025550195----http://169.254.169.254/latest/meta-data")
check("SSRF metadata rejected", code == 400, d)
os.environ["SMSGATE_RELAY_ALLOW_PRIVATE"] = "1"

# ---------- from pool ----------
now = int(time.time())
with db.tx() as c:
    c.execute("INSERT INTO numbers(phone,country,provider,sms_url,status,created_at) "
              "VALUES('+12025550111','us','pool',?,'available',?)", (M + "/sms?token=ok", now))
r = adm.post(B + "/admin/api/relay/from_pool", json={"count": 5}, headers={"X-CSRF": csrf})
d = r.json()
check("from_pool ok", r.status_code == 200 and d.get("count") == 1, d)
st = db.q1("SELECT status FROM numbers WHERE phone='+12025550111'")["status"]
check("pool number marked relay", st == "relay", st)

# ---------- reset / export / delete ----------
r = adm.post(B + "/admin/api/relay/%d/action" % rid, json={"action": "reset"}, headers={"X-CSRF": csrf})
d = r.json()
check("reset returns new line", d.get("ok") and "token=" in (d.get("line") or ""), d)
check("old token dead after reset", requests.get(B + "/smsrecord", params={"token": tok1}).status_code == 404)
newtok = d["line"].split("token=")[1]
check("new token works", requests.get(B + "/smsrecord", params={"token": newtok}).status_code == 200)

r = adm.get(B + "/admin/api/relay/export")
d = r.json()
check("export lines", d.get("ok") and d.get("count", 0) >= 3 and "卡号:+" in d.get("text", ""), d.get("count"))
r = adm.post(B + "/admin/api/relay/%d/action" % rid, json={"action": "delete"}, headers={"X-CSRF": csrf})
check("delete ok", r.json().get("ok") and requests.get(B + "/smsrecord", params={"token": newtok}).status_code == 404)

r = adm.post(B + "/admin/api/relay/create", json={"lines": ""}, headers={"X-CSRF": csrf})
check("empty lines rejected", r.status_code == 400, r.status_code)

# ---------- bad token spray guard (shared across workers) ----------
db.set_setting("relay_bad_rate", "2")
db.conn().execute("DELETE FROM rl WHERE bucket LIKE 'relaybad:%'")
bad = [requests.get(B + "/smsrecord", params={"token": "deadbeef" * 4}).status_code for _ in range(4)]
check("bad token spray limited", bad[0] == 404 and bad.count(429) >= 1, bad)
db.set_setting("relay_bad_rate", "300")
check("shared limiter rows", db.q1("SELECT COUNT(*) AS n FROM rl")["n"] >= 1)
r = adm.post(B + "/admin/api/relay/create",
             json={"lines": "+12025550197----http://127.0.0.1:%d/sms?token=ok" % MOCK_PORT},
             headers={"X-CSRF": csrf})
oktok = r.json()["items"][0]["token"]
check("valid token unaffected by bad bucket", requests.get(B + "/smsrecord", params={"token": oktok}).status_code == 200)

fails = [x for x in results if not x[1]]
print("\nSUMMARY %d/%d" % (len(results) - len(fails), len(results)))
if fails:
    print("FAILED:", [x[0] for x in fails])
    sys.exit(1)