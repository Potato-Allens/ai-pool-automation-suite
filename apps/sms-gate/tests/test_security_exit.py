# -*- coding: utf-8 -*-
"""核销即退出 + 后台防撞库 测试。"""
import os, sys, threading, time, json, re, shutil
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(BASE, "testdata_sec")
if os.path.isdir(TESTDATA):
    shutil.rmtree(TESTDATA, ignore_errors=True)
os.makedirs(TESTDATA, exist_ok=True)
os.environ["SMSGATE_ROOT"] = BASE
os.environ["SMSGATE_DATA"] = TESTDATA
os.environ["SMSGATE_PORT"] = "18771"
sys.path.insert(0, BASE)


class SMSHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"1|135790"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


threading.Thread(target=HTTPServer(("127.0.0.1", 18772), SMSHandler).serve_forever, daemon=True).start()

import uvicorn
from app.main import app
from app import security, db

db.init()
cfg = uvicorn.Config(app, host="127.0.0.1", port=18771, log_level="warning")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(60):
    if server.started:
        break
    time.sleep(0.1)

import requests
B = "http://127.0.0.1:18771"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:300]) if extra else ""))


def j(r):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:200]}

# ---------------- admin setup
adm = requests.Session()
j(adm.get(B + "/admin/api/bootstrap"))
token = open(os.path.join(TESTDATA, "setup.token"), encoding="utf-8").read().strip()
d = j(adm.post(B + "/admin/api/setup", json={"token": token, "username": "admin", "password": "StrongPass123"}))
secret = re.search(r"secret=([A-Z2-7]+)", d.get("otpauth") or "").group(1)
csrf = d.get("csrf", "")
j(adm.post(B + "/admin/api/setup/verify", json={"code": security.totp_at(secret)}, headers={"X-CSRF": csrf}))
csrf = j(adm.get(B + "/admin/api/me")).get("csrf", "")

# ---------------- card + number
d = j(adm.post(B + "/admin/api/cards/create", json={"count": 1, "max_codes": 1, "note": "sec"}, headers={"X-CSRF": csrf}))
key = d["keys"][0]
adm.post(B + "/admin/api/numbers/import",
         json={"country": "us", "lines": "+1 2025550199----http://127.0.0.1:18772/sms?token=x", "note": "sec"},
         headers={"X-CSRF": csrf})

pub = requests.Session()
d = j(pub.post(B + "/api/redeem", json={"key": key}))
pcsrf = d.get("csrf", "")
check("redeem ok", d.get("ok"), d.get("error"))
d = j(pub.post(B + "/api/number", json={"country": "us", "csrf": pcsrf}, headers={"X-CSRF": pcsrf}))
check("number ok", bool(d.get("number")), d)

r = pub.post(B + "/api/poll", json={"csrf": pcsrf}, headers={"X-CSRF": pcsrf})
d = j(r)
check("code received", d.get("code") == "135790", d)
check("force_exit flag", d.get("force_exit") is True, {k: d.get(k) for k in ("force_exit", "remaining", "grace")})
sc = r.headers.get("set-cookie", "")
check("cookie cleared by server", ("Max-Age=0" in sc) or ("sg_card=" in sc and "sg_card=;" in sc), sc)
check("no-store on code response", "no-store" in (r.headers.get("cache-control") or ""), r.headers.get("cache-control"))

r1 = pub.get(B + "/api/session")
check("session revoked after redeem", r1.status_code == 401, r1.status_code)
r2 = pub.post(B + "/api/poll", json={"csrf": pcsrf}, headers={"X-CSRF": pcsrf})
check("poll blocked after redeem", r2.status_code == 401, r2.status_code)
r3 = pub.post(B + "/api/number", json={"country": "us", "csrf": pcsrf}, headers={"X-CSRF": pcsrf})
check("number blocked after redeem", r3.status_code == 401, r3.status_code)
r4 = pub.post(B + "/api/redeem", json={"key": key})
check("redeem blocked after redeem", r4.status_code in (403, 409), (r4.status_code, j(r4).get("error")))

# ---------------- admin credential stuffing guard
IP_A = {"X-Forwarded-For": "10.20.30.40"}
bad = None
for i in range(5):
    bad = requests.post(B + "/admin/api/login", json={"username": "admin", "password": "wrong-%d" % i}, headers=IP_A)
check("5 bad logins rejected", bad.status_code in (401, 429), (bad.status_code, j(bad).get("error")))
db.conn().execute("UPDATE ips SET blocked_until=0 WHERE ip='10.20.30.40'")
r = requests.post(B + "/admin/api/login", json={"username": "admin", "password": "StrongPass123"}, headers=IP_A)
check("correct password blocked while locked", r.status_code == 429 and (j(r).get("retry_after") or 0) > 0,
      (r.status_code, j(r).get("error")))

db.conn().execute("DELETE FROM login_guard WHERE key='u:admin'")
db.conn().execute("DELETE FROM login_guard WHERE key='ip:10.20.30.40'")
db.conn().execute("DELETE FROM events WHERE kind='admin_login_fail' AND ip='10.20.30.40'")
db.conn().execute("UPDATE ips SET blocked_until=0 WHERE ip='10.20.30.40'")
r = requests.post(B + "/admin/api/login", json={"username": "admin", "password": "StrongPass123"}, headers=IP_A)
check("login works after lock expiry", r.status_code == 200 and j(r).get("need_totp"), (r.status_code, j(r).get("error")))

# Disable the legacy whole-IP auto-ban so the per-key guard is what gets tested.
db.set_setting("auto_ban_enabled", "0")
IP_B = {"X-Forwarded-For": "10.20.30.41"}
for i in range(10):
    requests.post(B + "/admin/api/login", json={"username": "user%d" % i, "password": "x"}, headers=IP_B)
r = requests.post(B + "/admin/api/login", json={"username": "admin", "password": "StrongPass123"}, headers=IP_B)
check("ip-level lock triggers", r.status_code == 429 and (j(r).get("retry_after") or 0) > 0,
      (r.status_code, j(r).get("error")))
db.conn().execute("DELETE FROM login_guard WHERE key LIKE 'ip:10.20.30.41'")
db.conn().execute("DELETE FROM events WHERE kind='admin_login_fail' AND ip='10.20.30.41'")

# ---------------- ip allowlist
db.set_setting("admin_ip_allow", "10.9.9.9")
r = requests.post(B + "/admin/api/login", json={"username": "admin", "password": "StrongPass123"},
                  headers={"X-Forwarded-For": "10.1.1.1"})
check("allowlist blocks other ip", r.status_code == 403, (r.status_code, j(r).get("error")))
r = requests.post(B + "/admin/api/login", json={"username": "admin", "password": "StrongPass123"},
                  headers={"X-Forwarded-For": "10.9.9.9"})
check("allowlist lets listed ip in", r.status_code == 200, (r.status_code, j(r).get("error")))
db.set_setting("admin_ip_allow", "")

# ---------------- totp brute force
s = requests.Session()
h = {"X-Forwarded-For": "10.20.30.42"}
d = j(s.post(B + "/admin/api/login", json={"username": "admin", "password": "StrongPass123"}, headers=h))
tcsrf = d.get("csrf", "")
last = None
for i in range(5):
    last = s.post(B + "/admin/api/login/totp", json={"code": "000000", "csrf": tcsrf},
                  headers={"X-CSRF": tcsrf, **h})
d5 = j(last)
check("totp failures lock", last.status_code in (401, 429) and (d5.get("retry_after") or 0) > 0,
      (last.status_code, d5.get("error")))
db.set_setting("auto_ban_enabled", "1")
check("pending admin session dropped", s.get(B + "/admin/api/me").status_code == 401,
      s.get(B + "/admin/api/me").status_code)

# ---------------- exit grace setting
db.set_setting("exit_grace_sec", "3")
d = j(adm.post(B + "/admin/api/cards/create", json={"count": 1, "max_codes": 1, "note": "sec2"}, headers={"X-CSRF": csrf}))
key2 = d["keys"][0]
adm.post(B + "/admin/api/numbers/import",
         json={"country": "us", "lines": "+1 2025550198----http://127.0.0.1:18772/sms?token=y", "note": "sec2"},
         headers={"X-CSRF": csrf})
pub2 = requests.Session()
d = j(pub2.post(B + "/api/redeem", json={"key": key2}))
c2 = d.get("csrf", "")
pub2.post(B + "/api/number", json={"country": "us", "csrf": c2}, headers={"X-CSRF": c2})
d = j(pub2.post(B + "/api/poll", json={"csrf": c2}, headers={"X-CSRF": c2}))
check("grace setting honoured", d.get("grace") == 3, d.get("grace"))

print("\\n=== %d/%d passed ===" % (sum(1 for r in results if r[1]), len(results)))
failed = [r[0] for r in results if not r[1]]
if failed:
    print("FAILED:", failed)
    raise SystemExit(1)

