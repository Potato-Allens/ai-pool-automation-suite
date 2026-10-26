# -*- coding: utf-8 -*-
"""Offline checks for the contract drift detector (no network, no token, no SMS).

The mock serves the same 0-cost surface the real auth server exposes (health,
openapi, agreements, captcha-config, sms-config, /login form) and a mutation
knob per class of drift, so every verdict -- critical, warning, clean, cannot
judge -- is exercised against a server that really answers HTTP.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("VMAX_NOPACE", "1")

import vmax_contract as C  # noqa: E402

fails = []
MUT = {}


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " | %s" % (detail,)))
    if not cond:
        fails.append(name)


def reset():
    MUT.clear()
    MUT.update({
        "health": "ok",          # ok | down
        "ready": "ready",        # ready | starting
        "scene": "leukcmsx",     # captcha scene for dreammaker
        "drop_path": None,
        "add_path": False,
        "rename": None,          # (old_field, new_field) on /api/auth/register
        "require_extra": None,
        "type_change": None,     # (field, new_type)
        "ag_version": "2026.09.30",
        "ag_drop": None,
        "login_drop": None,
        "codes_drop": None,      # code removed from /register
        "sms_enabled": True,
    })


def _op(fields=None, required=None, codes=("200", "400"), params=None):
    o = {"responses": {c: {"description": c} for c in codes}}
    if params:
        o["parameters"] = params
    if fields is not None:
        props = {}
        for k, v in fields.items():
            props[k] = {"type": "array", "items": {"type": "integer"}} if v == "array" else {"type": v}
        o["requestBody"] = {"content": {"application/json": {"schema": {
            "type": "object", "required": list(required or []), "properties": props}}}}
    return o


def build_doc():
    paths = {
        "/api/auth/agreements": {"get": _op(params=[{"name": "types", "in": "query"}])},
        "/api/auth/captcha-config": {"get": _op(params=[{"name": "platform", "in": "query"}])},
        "/api/auth/sms-config": {"get": _op()},
        "/api/auth/register": {"post": _op(
            {"phone": "string", "sms_code": "string", "username": "string", "password": "string",
             "accepted_agreement_ids": "array", "captcha_verify_param": "string"},
            ["phone", "sms_code", "username", "password"],
            ("200", "400", "409", "422"))},
        "/api/auth/login": {"post": _op({"username": "string", "password": "string"},
                                        ["username", "password"])},
        "/api/auth/login-sms": {"post": _op({"phone": "string", "sms_code": "string"},
                                            ["phone", "sms_code"])},
        "/api/auth/oauth/token": {"post": _op({"grant_type": "string", "code": "string",
                                               "client_id": "string", "code_verifier": "string"},
                                              ["grant_type", "code"])},
        "/api/auth/reset-password": {"post": _op({"phone": "string", "sms_code": "string"},
                                                 ["phone", "sms_code"])},
        "/api/auth/reset-password/send-code": {"post": _op({"phone": "string"}, ["phone"])},
        "/register": {"post": _op({"phone": "string", "sms_code": "string"}, ["phone", "sms_code"],
                                  ("303", "400"))},
        "/register/send-code": {"post": _op({"phone": "string"}, ["phone"],
                                            params=[{"name": "origin", "in": "header"}])},
        "/login": {"get": _op(), "post": _op()},
        "/session": {"get": _op()},
        "/health/live": {"get": _op()},
        "/terms": {"get": _op()},
    }
    reg = paths["/api/auth/register"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    if MUT.get("rename"):
        old, new = MUT["rename"]
        if old in reg["properties"]:
            reg["properties"][new] = reg["properties"].pop(old)
        reg["required"] = [new if f == old else f for f in reg["required"]]
    if MUT.get("type_change"):
        f, t = MUT["type_change"]
        reg["properties"][f] = {"type": t}
    if MUT.get("require_extra"):
        reg["required"] = sorted(set(reg["required"]) | {MUT["require_extra"]})
    if MUT.get("codes_drop"):
        paths["/register"]["post"]["responses"].pop(MUT["codes_drop"], None)
    if MUT.get("drop_path"):
        paths.pop(MUT["drop_path"], None)
    if MUT.get("add_path"):
        paths["/api/auth/brand-new"] = {"post": _op({"token": "string"})}
    return {"openapi": "3.1.0", "info": {"title": "vobilemax-auth", "version": "0.1.0"},
            "paths": paths}


def login_html(drop=None):
    inputs = [("phone", "tel"), ("sms_code", "text"), ("username", "text"),
              ("password", "password"), ("confirm_password", "password"),
              ("captcha_platform", "hidden"), ("captcha_verify_param", "hidden"),
              ("state", "hidden"), ("code_challenge", "hidden")]
    if drop:
        inputs = [x for x in inputs if x[0] != drop]
    body = "".join('<input type="%s" name="%s" id="%s" />' % (t, n, n) for n, t in inputs)
    return ("<html><head><script src=\"/static/captcha.js\"></script>"
            "<script src=\"/static/login-views.js\"></script></head>"
            "<body><div id=\"login-error\"></div><form>%s</form></body></html>" % body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        blob = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p == "/health/live":
            if MUT["health"] == "down":
                return self._send(503, {"status": "down"})
            return self._send(200, {"status": "ok"})
        if p == "/health/ready":
            if MUT["ready"] != "ready":
                return self._send(503, {"status": MUT["ready"]})
            return self._send(200, {"status": "ready"})
        if p == "/openapi.json":
            return self._send(200, build_doc())
        if p == "/api/auth/agreements":
            items = [{"id": 12, "type": "terms", "version": MUT["ag_version"],
                      "title": "\u7528\u6237\u534f\u8bae", "content": "terms body " * 40},
                     {"id": 13, "type": "privacy", "version": MUT["ag_version"],
                      "title": "\u9690\u79c1\u653f\u7b56", "content": "privacy body " * 40}]
            if MUT["ag_drop"]:
                items = [x for x in items if str(x["id"]) != str(MUT["ag_drop"])]
            return self._send(200, items)
        if p == "/api/auth/captcha-config":
            plat = (q.get("platform") or ["dreammaker"])[0]
            scene = MUT["scene"] if plat == "dreammaker" else "17rt8hvk"
            return self._send(200, {"enabled": True, "region": "cn",
                                    "prefix": "8vo785", "scene_id": scene})
        if p == "/api/auth/sms-config":
            return self._send(200, {"enabled": bool(MUT["sms_enabled"])})
        if p == "/session":
            return self._send(200, {"authenticated": False})
        if p == "/login":
            return self._send(200, login_html(MUT["login_drop"]).encode(), "text/html")
        return self._send(404, {"detail": "not found"})


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def run(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = C.main(args)
    return rc, buf.getvalue()


def main():
    reset()
    srv, base = start()
    try:
        with tempfile.TemporaryDirectory() as td:
            base_fp = os.path.join(td, "contract.json")

            print("[0] snapshot: compact, complete, no secrets")
            rc, out = run(["snapshot", "--base", base, "--out", base_fp])
            raw = open(base_fp, encoding="utf-8").read()
            doc = json.loads(raw)
            check("snapshot exit 0", rc == 0, out)
            check("version + base recorded", doc["version"] == 1 and doc["base"] == base, doc.get("version"))
            check("all six probes present",
                  all(k in doc for k in ("health", "readiness", "openapi", "agreements",
                                         "captcha", "login")), sorted(doc))
            check("readiness recorded", doc["readiness"]["status"] == "ready", doc.get("readiness"))
            check("41-ish path map with methods", doc["openapi"]["paths"]["/login"] == ["GET", "POST"],
                  doc["openapi"]["paths"].get("/login"))
            check("request fields are typed",
                  doc["openapi"]["ops"]["POST /api/auth/register"]["fields"]["sms_code"] == "string")
            check("required list kept",
                  "sms_code" in doc["openapi"]["ops"]["POST /api/auth/register"]["required"])
            check("response codes kept",
                  "409" in doc["openapi"]["ops"]["POST /api/auth/register"]["codes"],
                  doc["openapi"]["ops"]["POST /api/auth/register"]["codes"])
            check("agreements carry a content hash, not the text",
                  '"content":' not in raw and doc["agreements"]["items"][0]["content_len"] > 0)
            check("fingerprint stays small (<40 KB)", len(raw) < 40000, len(raw))
            check("no cookie or token in the file",
                  "access_token" not in raw and "Set-Cookie" not in raw)
            check("captcha scene recorded",
                  doc["captcha"]["dreammaker"]["scene_id"] == "leukcmsx")
            check("login form inputs recorded", "sms_code" in doc["login"]["inputs"])

            print("[1] an unchanged server is clean")
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 0", rc == 0, out)
            check("says no drift", "no drift" in out, out)
            check("no CRIT line", "[CRIT]" not in out, out)

            print("[2] the captcha scene moving is critical")
            reset(); MUT["scene"] = "zzzzzzz"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 1", rc == 1, out)
            check("names the captcha scene", "scene_id" in out and "leukcmsx" in out, out)

            print("[3] a renamed request field is critical")
            reset(); MUT["rename"] = ("sms_code", "code")
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 1", rc == 1, out)
            check("names the field that vanished", "sms_code" in out, out)
            check("names the field that appeared", "code" in out, out)

            print("[4] a new required field is critical")
            reset(); MUT["require_extra"] = "device_id"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 1", rc == 1, out)
            check("says now requires", "now requires" in out, out)

            print("[5] a field changing type is critical")
            reset(); MUT["type_change"] = ("phone", "integer")
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 1", rc == 1, out)
            check("names the type move", "string -> integer" in out, out)

            print("[6] losing a path we call is critical, a page we do not is a warning")
            reset(); MUT["drop_path"] = "/register"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("critical path gone -> exit 1", rc == 1, out)
            check("message says removed", "path removed /register" in out, out)
            reset(); MUT["drop_path"] = "/terms"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("unused path gone -> exit 0", rc == 0, out)
            check("but it is still reported", "[WARN]" in out and "/terms" in out, out)

            print("[7] a brand new endpoint is a warning, and --strict makes it bite")
            reset(); MUT["add_path"] = True
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 0 without --strict", rc == 0, out)
            check("reported as added", "path added" in out and "brand-new" in out, out)
            rc, out = run(["check", "--base", base, "--baseline", base_fp, "--strict"])
            check("--strict exits 1", rc == 1, out)

            print("[8] a new agreement version is a warning, a lost id is critical")
            reset(); MUT["ag_version"] = "2027.01.01"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("version bump -> exit 0", rc == 0, out)
            check("version shown", "2026.09.30 -> 2027.01.01" in out, out)
            reset(); MUT["ag_drop"] = 13
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("id gone -> exit 1", rc == 1, out)
            check("id named", "id 13" in out, out)

            print("[9] health going down is critical")
            reset(); MUT["health"] = "down"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 1", rc == 1, out)
            check("health line names 503", "503" in out, out)

            print("[10] sms-config disabling is critical")
            reset(); MUT["sms_enabled"] = False
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 1", rc == 1, out)
            check("sms_config named", "sms_config" in out, out)

            print("[11] the login form losing an input is critical")
            reset(); MUT["login_drop"] = "sms_code"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 1", rc == 1, out)
            check("input named", "input removed 'sms_code'" in out, out)

            print("[12] a 303 that stops being documented is critical")
            reset(); MUT["codes_drop"] = "303"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 1", rc == 1, out)
            check("code named", "response code 303" in out, out)

            print("[13] --json and --out carry the same verdict, without secrets")
            reset(); MUT["scene"] = "zzzzzzz"
            rp = os.path.join(td, "result.json")
            rc, out = run(["check", "--base", base, "--baseline", base_fp,
                           "--out", rp, "--quiet"])
            res = json.loads(open(rp, encoding="utf-8").read())
            check("exit 1", rc == 1, out)
            check("quiet prints nothing", out.strip() == "", out[:200])
            rc2, out2 = run(["check", "--base", base, "--baseline", base_fp, "--json", "--quiet"])
            check("--json alone is parseable", json.loads(out2)["verdict"] == "critical", out2[:120])
            check("result has critical/warn/info", all(k in res for k in ("critical", "warn", "info")))
            check("verdict is critical", res["verdict"] == "critical", res["verdict"])
            check("ok is false", res["ok"] is False)
            check("no secrets in the result",
                  "access_token" not in json.dumps(res) and "password" not in json.dumps(res["critical"]))

            print("[14] diff works offline on two saved fingerprints")
            reset()
            a = os.path.join(td, "a.json"); b = os.path.join(td, "b.json")
            run(["snapshot", "--base", base, "--out", a])
            MUT["scene"] = "zzzzzzz"
            run(["snapshot", "--base", base, "--out", b])
            rc, out = run(["diff", a, b])
            check("exit 1", rc == 1, out)
            check("same message as check", "scene_id" in out, out)
            rc, out = run(["diff", a, a])
            check("identical files exit 0", rc == 0, out)

            print("[15] bad inputs are exit 2, not a false alarm")
            rc, out = run(["check", "--base", base, "--baseline", os.path.join(td, "nope.json")])
            check("missing baseline -> 2", rc == 2, out)
            broken = os.path.join(td, "broken.json")
            open(broken, "w", encoding="utf-8").write("{not json")
            rc, out = run(["check", "--base", base, "--baseline", broken])
            check("unparseable baseline -> 2", rc == 2, out)
            s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
            rc, out = run(["check", "--base", "http://127.0.0.1:%d" % dead,
                           "--baseline", base_fp, "--platforms", "dreammaker"])
            check("dead site -> 2 (cannot judge)", rc == 2, out)
            check("and it does not cry wolf", "[CRIT] openapi: path removed" not in out, out[:300])

            print("[16] capture knobs: --no-login skips the form probe")
            reset(); MUT["ready"] = "starting"
            rc, out = run(["check", "--base", base, "--baseline", base_fp])
            check("exit 0", rc == 0, out)
            check("reported as WARN", "[WARN] readiness" in out, out)
            check("nothing critical", "[CRIT]" not in out, out)
            rc, out = run(["check", "--base", base, "--baseline", base_fp, "--strict"])
            check("--strict turns it into exit 1", rc == 1, out)

            print("[17] readiness flapping is a warning, never a blocker")
            reset()
            fp = C.capture(C.VmaxAuth(base=base, timeout=5), with_login=False)
            check("no-login capture has no login block", "login" not in fp, sorted(fp))
            check("but keeps the rest", fp["openapi"]["count"] >= 15, fp["openapi"].get("count"))
    finally:
        srv.shutdown()
        srv.server_close()

    print("ALL CONTRACT TESTS PASS" if not fails else "FAILURES: %s" % (fails,))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
