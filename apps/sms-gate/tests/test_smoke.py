import os, sys, threading, time, json, re, shutil
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(BASE, "testdata")
if os.path.isdir(TESTDATA):
    shutil.rmtree(TESTDATA, ignore_errors=True)
os.makedirs(TESTDATA, exist_ok=True)
os.environ["SMSGATE_ROOT"] = BASE
os.environ["SMSGATE_DATA"] = TESTDATA
os.environ["SMSGATE_PORT"] = "8899"
sys.path.insert(0, BASE)

class SMSHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"1|654321"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

sms_srv = HTTPServer(("127.0.0.1", 18898), SMSHandler)
threading.Thread(target=sms_srv.serve_forever, daemon=True).start()

import uvicorn
from app.main import app
from app import security, db

db.init()
cfg = uvicorn.Config(app, host="127.0.0.1", port=8899, log_level="warning")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(50):
    if server.started:
        break
    time.sleep(0.1)

import requests
B = "http://127.0.0.1:8899"
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)) if extra else ""))

def j(resp):
    try:
        return resp.json()
    except Exception:
        return {"_raw": resp.text[:300], "_status": resp.status_code}

hr = requests.get(B + "/api/health")
check("health", j(hr).get("ok"), (hr.status_code, hr.text[:120]))
check("public page", requests.get(B + "/").status_code == 200)
check("admin page", requests.get(B + "/admin").status_code == 200)
check("login page", requests.get(B + "/admin/login").status_code == 200)
check("setup page", requests.get(B + "/admin/setup").status_code == 200)
check("css", requests.get(B + "/static/style.css").status_code == 200)

adm = requests.Session()
boot = j(adm.get(B + "/admin/api/bootstrap"))
check("needs setup", boot.get("needs_setup") is True, boot)

token = open(os.path.join(TESTDATA, "setup.token"), encoding="utf-8").read().strip()
r = adm.post(B + "/admin/api/setup", json={"token": "wrong", "username": "admin", "password": "Abcdef12345"})
check("bad setup token rejected", r.status_code == 403, r.text[:120])

r = adm.post(B + "/admin/api/setup", json={"token": token, "username": "admin", "password": "StrongPass123"})
d = j(r)
check("setup ok", d.get("ok"), d.get("error"))
check("qr svg", "<svg" in (d.get("qr_svg") or ""), len(d.get("qr_svg") or ""))
sec = re.search(r"secret=([A-Z2-7]+)", d.get("otpauth") or "")
check("otpauth secret", bool(sec))
secret = sec.group(1) if sec else ""
csrf = d.get("csrf", "")
code = security.totp_at(secret)
r = adm.post(B + "/admin/api/setup/verify", json={"code": code}, headers={"X-CSRF": csrf})
d = j(r)
check("totp bind", d.get("ok"), d.get("error"))

# session cookie is now active
me = j(adm.get(B + "/admin/api/me"))
check("admin me", me.get("ok") and me.get("username") == "admin", me)
csrf = me.get("csrf", "")

r = adm.post(B + "/admin/api/cards/create", json={"count": 2, "max_codes": 1, "note": "smoke"}, headers={"X-CSRF": csrf})
d = j(r)
check("create cards", d.get("ok") and len(d.get("keys", [])) == 2, d.get("error"))
keys = d.get("keys", [])
check("links", all("/c/" in x for x in d.get("links", [])), d.get("links"))

lines = "+1 2025550101----http://127.0.0.1:18898/sms?token=a\n+1 2025550102|http://127.0.0.1:18898/sms?token=b\n"
r = adm.post(B + "/admin/api/numbers/import", json={"country": "us", "lines": lines, "note": "smoke"}, headers={"X-CSRF": csrf})
d = j(r)
check("import numbers", d.get("ok") and d.get("added") == 2, d)

# settings
r = adm.post(B + "/admin/api/settings", json={"site_url": "https://sms.example.com", "poll_interval": "15", "allow_countries": "us,gb"}, headers={"X-CSRF": csrf})
check("save settings", j(r).get("ok"), r.text[:200])
r = adm.post(B + "/admin/api/provider/test", json={}, headers={"X-CSRF": csrf})
check("provider test", j(r).get("ok"), j(r).get("message"))

# public flow
pub = requests.Session()
r = pub.post(B + "/api/redeem", json={"key": keys[0]})
d = j(r)
check("redeem", d.get("ok"), d.get("error"))
pcsrf = d.get("csrf", "")
s = j(pub.get(B + "/api/session"))
check("session", s.get("ok") and s.get("csrf"), s.get("error"))
check("countries", any(c["code"] == "us" for c in s.get("countries", [])), s.get("countries"))
pcsrf = s.get("csrf", "")
r = pub.post(B + "/api/number", json={"country": "us", "csrf": pcsrf}, headers={"X-CSRF": pcsrf})
d = j(r)
check("take number", d.get("ok") and d.get("number", {}).get("phone"), d.get("error"))
r = pub.post(B + "/api/poll", json={"csrf": pcsrf}, headers={"X-CSRF": pcsrf})
d = j(r)
check("poll code", d.get("state") == "code" and d.get("code") == "654321", d)
r = pub.post(B + "/api/poll", json={"csrf": pcsrf}, headers={"X-CSRF": pcsrf})
d = j(r)
# 一卡一码：核销后该卡密的所有会话会被销毁，旧 cookie 必须立刻失效。
check("session revoked after consume", r.status_code == 401, {"status": r.status_code, "body": d})

# used card cannot redeem
r2 = requests.post(B + "/api/redeem", json={"key": keys[0]})
check("used card blocked", r2.status_code == 409, r2.status_code)

# second card, card page by link
r3 = requests.get(B + "/c/" + keys[1])
check("card link page", r3.status_code == 200 and keys[1] in r3.text)

# admin data lists
for path, key in [("cards", "items"), ("numbers", "items"), ("codes", "items"), ("redemptions", "items"), ("usage", "items"), ("audit", "items"), ("ips", "items")]:
    d = j(adm.get(B + "/admin/api/" + path))
    # usage 列表在“核销即注销”之后可能为空，这里只校验接口可用。
    rows = d.get(key) if isinstance(d.get(key), list) else []
    check("list " + path, d.get("ok") and (len(rows) > 0 or path == "usage"), d.get("total"))
    if path == "codes":
        check("code row", any(x.get("code") == "654321" for x in d.get("items", [])))

ov = j(adm.get(B + "/admin/api/overview"))
check("overview", ov.get("ok") and ov["stats"]["codes_total"] >= 1, ov.get("stats"))

# login flow
sess = requests.Session()
r = sess.post(B + "/admin/api/login", json={"username": "admin", "password": "wrongpass"})
check("bad login", r.status_code == 401, r.status_code)
r = sess.post(B + "/admin/api/login", json={"username": "admin", "password": "StrongPass123"})
d = j(r)
check("login step1", d.get("need_totp"), d)
lcsrf = d.get("csrf", "")
c = security.totp_at(secret)
r = sess.post(B + "/admin/api/login/totp", json={"code": "000000"}, headers={"X-CSRF": lcsrf})
check("bad totp", r.status_code == 401, r.status_code)
c = security.totp_at(secret)
r = sess.post(B + "/admin/api/login/totp", json={"code": c}, headers={"X-CSRF": lcsrf})
check("login totp", j(r).get("ok"), r.text[:200])

# csrf enforcement
r = sess.post(B + "/admin/api/cards/create", json={"count": 1})
check("csrf enforced", r.status_code == 403, r.status_code)

# security headers
h = requests.get(B + "/").headers
check("csp header", "default-src 'self'" in h.get("content-security-policy", ""), h.get("content-security-policy"))
check("nosniff", h.get("x-content-type-options") == "nosniff")

failed = [x for x in results if not x[1]]
print("\n=== %d/%d passed ===" % (len(results) - len(failed), len(results)))
if failed:
    print("FAILED:", [x[0] for x in failed])
    sys.exit(1)