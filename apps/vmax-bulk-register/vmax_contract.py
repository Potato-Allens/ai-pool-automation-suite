# -*- coding: utf-8 -*-
"""Contract drift detector for auth.vobilemax.cn -- 0 token, 0 SMS, 0 account.

Everything the batch toolkit relies on is an unpublished contract: 41 openapi
paths, a dozen request field names, the Aliyun captcha scene id, two agreement
ids, and the input names of the /login form.  A silent rename on the server
(say ``sms_code`` -> ``code``) does not fail loudly -- it fails in the middle of
a paid batch, as "every number errors".

    python vmax_contract.py snapshot --out contract.json     # freeze today
    python vmax_contract.py check --baseline contract.json   # before a run
    python vmax_contract.py diff old.json new.json           # offline

Exit codes: 0 no drift (warnings are fine), 1 critical drift (or any drift
with ``--strict``), 2 could not capture / baseline unreadable.

Critical means "the batch would break or silently change meaning": a path we
call disappears, a field we send disappears or changes type, a required field
appears, the captcha scene/prefix changes, agreements lose an id, the /login
form loses an input, or the health probe stops answering ok.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from vmax_client import VmaxAuth, pkce_pair  # noqa: E402

VERSION = 1
METHODS = ("get", "post", "put", "patch", "delete")

# Paths the toolkit actually calls.  Losing one of these is critical; a path we
# never touch is only a warning when it disappears.
CRITICAL_PATHS = {
    "/api/auth/agreements",
    "/api/auth/captcha-config",
    "/api/auth/sms-config",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/login-sms",
    "/api/auth/oauth/token",
    "/api/auth/reset-password",
    "/api/auth/reset-password/send-code",
    "/register",
    "/register/send-code",
    "/login",
    "/session",
}

# Request bodies worth fingerprinting field by field.
CRITICAL_OPS = [
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/login-sms"),
    ("POST", "/api/auth/oauth/token"),
    ("POST", "/api/auth/reset-password"),
    ("POST", "/api/auth/reset-password/send-code"),
    ("POST", "/register"),
    ("POST", "/register/send-code"),
    ("GET", "/api/auth/agreements"),
    ("GET", "/api/auth/captcha-config"),
]

# Inputs the client posts by name; the form losing one breaks signup.
CORE_LOGIN_INPUTS = {
    "phone", "password", "sms_code", "username",
    "captcha_platform", "captcha_verify_param",
}
CAPTCHA_KEYS = ("enabled", "region", "prefix", "scene_id")


def _sha12(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:12]


def _type_of(sch: Any) -> str:
    """A short, stable type name: 'string', 'array<string>', 'RegisterRequest'."""
    if not isinstance(sch, dict):
        return "?"
    ref = sch.get("$ref")
    if ref:
        return str(ref).split("/")[-1]
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(sch.get(key), list) and sch[key]:
            return _type_of(sch[key][0])
    t = sch.get("type")
    if t == "array":
        return "array<%s>" % _type_of(sch.get("items") or {})
    if not t and "properties" in sch:
        return "object"
    return str(t or "?")


def _read_openapi(cli: VmaxAuth) -> dict:
    j = cli.s.get(cli.base + "/openapi.json", timeout=30).json()
    paths: dict[str, list[str]] = {}
    for p, ops in (j.get("paths") or {}).items():
        methods = sorted(k.upper() for k in ops if k.lower() in METHODS)
        if methods:
            paths[p] = methods
    ops: dict[str, dict] = {}
    doc_paths = j.get("paths") or {}
    comps = (j.get("components") or {}).get("schemas") or {}
    for method, path in CRITICAL_OPS:
        op = (doc_paths.get(path) or {}).get(method.lower()) or {}
        body = {
            "codes": sorted(str(c) for c in (op.get("responses") or {})),
            "params": [],
            "required": [],
            "fields": {},
        }
        for prm in op.get("parameters") or []:
            if isinstance(prm, dict) and prm.get("name"):
                body["params"].append({"name": prm["name"], "in": prm.get("in"),
                                       "required": bool(prm.get("required"))})
        body["params"].sort(key=lambda x: (str(x.get("in")), str(x.get("name"))))
        content = ((op.get("requestBody") or {}).get("content") or {})
        sch = None
        for ct in ("application/json", "application/x-www-form-urlencoded",
                   "multipart/form-data"):
            if ct in content:
                sch = content[ct].get("schema")
                break
        if isinstance(sch, dict) and "$ref" in sch:
            sch = comps.get(str(sch["$ref"]).split("/")[-1]) or {}
        if isinstance(sch, dict):
            body["required"] = sorted(sch.get("required") or [])
            body["fields"] = {k: _type_of(v) for k, v in sorted((sch.get("properties") or {}).items())}
        ops["%s %s" % (method, path)] = body
    return {
        "count": len(paths),
        "title": (j.get("info") or {}).get("title"),
        "version": (j.get("info") or {}).get("version"),
        "paths": paths,
        "ops": ops,
    }


def _read_agreements(cli: VmaxAuth) -> dict:
    items = []
    for a in cli.agreements():
        content = str(a.get("content") or "")
        items.append({
            "id": a.get("id"),
            "type": a.get("type"),
            "version": a.get("version"),
            "title": a.get("title"),
            "content_sha": _sha12(content.encode("utf-8")),
            "content_len": len(content),
        })
    items.sort(key=lambda x: str(x.get("id")))
    return {"ids": [str(i["id"]) for i in items], "items": items}


def _read_login(cli: VmaxAuth) -> dict:
    _, challenge = pkce_pair()
    url = VmaxAuth.login_url(challenge, state=secrets.token_urlsafe(16),
                             base=cli.base, redirect_uri=cli.redirect_uri)
    r = cli.s.get(url, timeout=25)
    body = r.text
    inputs, hidden = set(), set()
    for tag in re.findall(r"<input\b[^>]*>", body):
        m = re.search(r'name="([^"]+)"', tag)
        if not m:
            continue
        inputs.add(m.group(1))
        if re.search(r'type="hidden"', tag):
            hidden.add(m.group(1))
    return {
        "status": r.status_code,
        "inputs": sorted(inputs),
        "hidden": sorted(hidden),
        "has_error_slot": 'id="login-error"' in body,
        "scripts": sorted(set(re.findall(r'src="([^"]+\.js[^"]*)"', body))),
    }


def capture(cli: VmaxAuth, platforms=("dreammaker", "max"), with_login=True) -> dict:
    """Read the public contract.  Never sends a code, never writes an account."""
    fp: dict[str, Any] = {"version": VERSION, "base": cli.base,
                          "taken_at": int(time.time()), "errors": []}

    def guard(name: str, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            fp["errors"].append({"step": name,
                                 "error": "%s: %s" % (type(e).__name__, str(e)[:200])})
            return None

    def health():
        r = cli.s.get(cli.base + "/health/live", timeout=15)
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        return {"code": r.status_code, "status": payload.get("status"),
                "ok": r.status_code == 200 and payload.get("status") == "ok"}

    def readiness():
        r = cli.s.get(cli.base + "/health/ready", timeout=15)
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        return {"code": r.status_code, "status": payload.get("status"),
                "ok": r.status_code == 200 and payload.get("status") == "ready"}

    fp["health"] = guard("health", health)
    fp["readiness"] = guard("readiness", readiness)
    fp["openapi"] = guard("openapi", lambda: _read_openapi(cli))
    fp["agreements"] = guard("agreements", lambda: _read_agreements(cli))
    fp["captcha"] = guard("captcha", lambda: {p: cli.captcha_config(p) for p in platforms})
    fp["sms_config"] = guard("sms_config", lambda: cli.sms_config())
    if with_login:
        fp["login"] = guard("login", lambda: _read_login(cli))
    return fp


def compare(old: dict, new: dict, strict: bool = False) -> dict:
    """Classify every difference.  Returns {critical, warn, info, error}."""
    crit: list[str] = []
    warn: list[str] = []
    info: list[str] = []

    def add(bucket: list, msg: str) -> None:
        if msg not in bucket:
            bucket.append(msg)

    if new.get("errors"):
        for e in new["errors"]:
            add(crit, "capture failed: %s (%s)" % (e.get("step"), e.get("error")))
    if old.get("base") and new.get("base") and old["base"] != new["base"]:
        add(crit, "base changed: %s -> %s" % (old["base"], new["base"]))

    # ---- health -----------------------------------------------------------
    h = new.get("health")
    if not h:
        add(crit, "health: no response")
    elif not h.get("ok"):
        add(crit, "health: %s %s (expected 200 ok)" % (h.get("code"), h.get("status")))

    r_old, r_new = old.get("readiness"), new.get("readiness")
    if r_old and not r_new:
        add(warn, "readiness: probe returned nothing")
    elif r_old and r_new and r_old.get("ok") and not r_new.get("ok"):
        add(warn, "readiness: %s %s (was ready; deploy or dependency flap?)" % (
            r_new.get("code"), r_new.get("status")))
    elif not r_old and r_new:
        add(info, "readiness: probe added")

    # ---- openapi paths ----------------------------------------------------
    o_paths = (old.get("openapi") or {}).get("paths") or {}
    n_paths = (new.get("openapi") or {}).get("paths") or {}
    for p in sorted(set(o_paths) - set(n_paths)):
        add(crit if p in CRITICAL_PATHS else warn, "openapi: path removed %s" % p)
    for p in sorted(set(n_paths) - set(o_paths)):
        add(warn, "openapi: path added %s %s" % (",".join(n_paths[p]), p))
    for p in sorted(set(o_paths) & set(n_paths)):
        gone = sorted(set(o_paths[p]) - set(n_paths[p]))
        grew = sorted(set(n_paths[p]) - set(o_paths[p]))
        if gone:
            add(crit if p in CRITICAL_PATHS else warn,
                "openapi: %s lost method %s" % (p, ",".join(gone)))
        if grew:
            add(info, "openapi: %s gained method %s" % (p, ",".join(grew)))
    if (old.get("openapi") or {}).get("count") != (new.get("openapi") or {}).get("count"):
        add(info, "openapi: path count %s -> %s" % (
            (old.get("openapi") or {}).get("count"), (new.get("openapi") or {}).get("count")))

    # ---- request bodies ---------------------------------------------------
    o_ops = (old.get("openapi") or {}).get("ops") or {}
    n_ops = (new.get("openapi") or {}).get("ops") or {}
    for key in sorted(set(o_ops) | set(n_ops)):
        a, b = o_ops.get(key), n_ops.get(key)
        if a is None:
            add(warn, "schema: new operation %s" % key)
            continue
        if b is None:
            add(crit, "schema: operation vanished %s" % key)
            continue
        for f in sorted(set(a.get("fields") or {}) - set(b.get("fields") or {})):
            add(crit, "schema: %s field removed %r" % (key, f))
        for f in sorted(set(b.get("fields") or {}) - set(a.get("fields") or {})):
            add(warn, "schema: %s field added %r" % (key, f))
        for f in sorted(set(a.get("fields") or {}) & set(b.get("fields") or {})):
            if a["fields"][f] != b["fields"][f]:
                add(crit, "schema: %s field %r type %s -> %s" % (
                    key, f, a["fields"][f], b["fields"][f]))
        req_a, req_b = set(a.get("required") or []), set(b.get("required") or [])
        for f in sorted(req_a - req_b):
            add(warn, "schema: %s field %r no longer required" % (key, f))
        for f in sorted(req_b - req_a):
            add(crit, "schema: %s now requires %r (client does not send it?)" % (key, f))
        for code in sorted(set(a.get("codes") or []) - set(b.get("codes") or [])):
            add(crit if code.startswith(("2", "3")) else warn,
                "schema: %s response code %s disappeared" % (key, code))
        for code in sorted(set(b.get("codes") or []) - set(a.get("codes") or [])):
            add(warn, "schema: %s response code %s appeared" % (key, code))
        pa = {(x.get("in"), x.get("name")): bool(x.get("required")) for x in a.get("params") or []}
        pb = {(x.get("in"), x.get("name")): bool(x.get("required")) for x in b.get("params") or []}
        for k in sorted(set(pa) - set(pb)):
            add(warn, "schema: %s query/header param removed %s" % (key, k[1]))
        for k in sorted(set(pb) - set(pa)):
            add(warn, "schema: %s query/header param added %s%s" % (
                key, k[1], " (required)" if pb[k] else ""))

    # ---- agreements -------------------------------------------------------
    o_ag = (old.get("agreements") or {})
    n_ag = (new.get("agreements") or {})
    o_ids, n_ids = set(o_ag.get("ids") or []), set(n_ag.get("ids") or [])
    for i in sorted(o_ids - n_ids):
        add(crit, "agreements: id %s disappeared (register must post accepted ids)" % i)
    for i in sorted(n_ids - o_ids):
        add(warn, "agreements: new id %s (may need to be accepted)" % i)
    o_items = {str(x.get("id")): x for x in o_ag.get("items") or []}
    n_items = {str(x.get("id")): x for x in n_ag.get("items") or []}
    for i in sorted(set(o_items) & set(n_items)):
        a, b = o_items[i], n_items[i]
        if a.get("version") != b.get("version"):
            add(warn, "agreements: id %s version %s -> %s" % (i, a.get("version"), b.get("version")))
        if a.get("content_sha") != b.get("content_sha"):
            add(warn, "agreements: id %s text changed (%s -> %s)" % (
                i, a.get("content_sha"), b.get("content_sha")))
        if a.get("type") != b.get("type"):
            add(crit, "agreements: id %s type %s -> %s" % (i, a.get("type"), b.get("type")))

    # ---- captcha ----------------------------------------------------------
    o_cap = old.get("captcha") or {}
    n_cap = new.get("captcha") or {}
    for plat in sorted(set(o_cap) | set(n_cap)):
        a, b = o_cap.get(plat), n_cap.get(plat)
        if a is None:
            add(warn, "captcha: platform %s newly present" % plat)
            continue
        if b is None:
            add(crit, "captcha: platform %s vanished" % plat)
            continue
        for k in CAPTCHA_KEYS:
            if a.get(k) != b.get(k):
                if k == "enabled" and b.get(k):
                    add(warn, "captcha[%s]: enabled %s -> %s" % (plat, a.get(k), b.get(k)))
                else:
                    add(crit, "captcha[%s]: %s %s -> %s" % (plat, k, a.get(k), b.get(k)))

    # ---- sms config -------------------------------------------------------
    a_sms = old.get("sms_config") or {}
    b_sms = new.get("sms_config") or {}
    if a_sms.get("enabled") is True and b_sms.get("enabled") is not True:
        add(crit, "sms_config: enabled -> %s" % b_sms.get("enabled"))
    for k in sorted(set(a_sms) | set(b_sms)):
        if a_sms.get(k) != b_sms.get(k):
            add(info, "sms_config: %s %s -> %s" % (k, a_sms.get(k), b_sms.get(k)))

    # ---- login form -------------------------------------------------------
    a_lg = old.get("login") or {}
    b_lg = new.get("login") or {}
    if a_lg and not b_lg:
        add(crit, "login: page unreachable")
    elif a_lg and b_lg:
        if b_lg.get("status") != 200:
            add(crit, "login: status %s (expected 200)" % b_lg.get("status"))
        ins_a, ins_b = set(a_lg.get("inputs") or []), set(b_lg.get("inputs") or [])
        for f in sorted(ins_a - ins_b):
            add(crit if f in CORE_LOGIN_INPUTS else warn, "login: input removed %r" % f)
        for f in sorted(ins_b - ins_a):
            add(warn, "login: input added %r" % f)
        if a_lg.get("has_error_slot") and not b_lg.get("has_error_slot"):
            add(warn, "login: error slot id=login-error disappeared")
        if (a_lg.get("scripts") or []) != (b_lg.get("scripts") or []):
            add(info, "login: script bundle changed")

    drift = bool(crit or warn)
    return {
        "critical": crit,
        "warn": warn,
        "info": info,
        "strict": bool(strict),
        "ok": not crit and not (strict and warn),
        "verdict": "critical" if crit else ("warn" if warn else "clean"),
        "drift": drift,
        "paths": (new.get("openapi") or {}).get("count"),
    }


def _render(result: dict, out=None) -> None:
    out = out or sys.stdout
    for msg in result["critical"]:
        print("[CRIT] " + msg, file=out)
    for msg in result["warn"]:
        print("[WARN] " + msg, file=out)
    for msg in result["info"]:
        print("[info] " + msg, file=out)
    if result["ok"]:
        print("[contract] no drift (%s paths)" % result.get("paths"), file=out)
    else:
        print("[contract] DRIFT: %d critical / %d warn (strict=%s, %s paths)" % (
            len(result["critical"]), len(result["warn"]),
            result["strict"], result.get("paths")), file=out)


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _save(path: str, data: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def _summary(fp: dict) -> str:
    op = fp.get("openapi") or {}
    ag = fp.get("agreements") or {}
    cap = (fp.get("captcha") or {}).get("dreammaker") or {}
    lg = fp.get("login") or {}
    return ("paths=%s agreements=%s scene=%s inputs=%d" % (
        op.get("count"), ",".join(ag.get("ids") or []) or "-",
        cap.get("scene_id") or "-", len(lg.get("inputs") or [])))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="auth.vobilemax.cn contract drift detector")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--base", default=None, help="auth base URL (env VMAX_BASE)")
        p.add_argument("--redirect", default=None)
        p.add_argument("--proxy", default=None)
        p.add_argument("--platforms", default="dreammaker,max",
                       help="comma separated captcha platforms to fingerprint")
        p.add_argument("--no-login", action="store_true", help="skip the /login form probe")

    sp = sub.add_parser("snapshot", help="capture the current contract into a baseline file")
    common(sp)
    sp.add_argument("--out", default="contract.json")
    sp.add_argument("--json", action="store_true", help="print the fingerprint")

    cp = sub.add_parser("check", help="compare the live contract against a baseline")
    common(cp)
    cp.add_argument("--baseline", required=True)
    cp.add_argument("--strict", action="store_true", help="treat warnings as drift")
    cp.add_argument("--out", default=None, help="write the diff result as JSON")
    cp.add_argument("--json", action="store_true")
    cp.add_argument("--quiet", action="store_true")

    dp = sub.add_parser("diff", help="compare two saved fingerprints offline")
    dp.add_argument("old")
    dp.add_argument("new")
    dp.add_argument("--strict", action="store_true")
    dp.add_argument("--out", default=None)
    dp.add_argument("--json", action="store_true")
    dp.add_argument("--quiet", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "diff":
        try:
            old, new = _load(args.old), _load(args.new)
        except Exception as e:  # noqa: BLE001
            print("[contract] cannot read fingerprints: %s: %s" % (type(e).__name__, e))
            return 2
        result = compare(old, new, strict=args.strict)
        if args.out:
            _save(args.out, result)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        if not args.quiet:
            _render(result)
        return 0 if result["ok"] else 1

    platforms = tuple(x.strip() for x in str(args.platforms).split(",") if x.strip())
    cli = VmaxAuth(proxy=args.proxy, base=args.base, redirect_uri=args.redirect, timeout=25)
    fp = capture(cli, platforms=platforms, with_login=not args.no_login)

    if args.cmd == "snapshot":
        _save(args.out, fp)
        if args.json:
            print(json.dumps(fp, ensure_ascii=False, indent=1, sort_keys=True))
        print("[contract] snapshot %s -> %s" % (_summary(fp), args.out))
        if fp.get("errors"):
            print("[contract] WARNING %d step(s) failed: %s" % (
                len(fp["errors"]), ", ".join(e["step"] for e in fp["errors"])))
            return 2
        return 0

    try:
        old = _load(args.baseline)
    except Exception as e:  # noqa: BLE001
        print("[contract] cannot read baseline %s: %s: %s" % (args.baseline, type(e).__name__, e))
        return 2
    hard = [e for e in (fp.get("errors") or []) if e.get("step") in ("health", "openapi")]
    if len(hard) >= 2:
        # Nothing to compare against: do not report the whole contract as drift.
        print("[contract] cannot judge: health and openapi did not answer (%s)" % (
            "; ".join(str(e.get("error"))[:80] for e in hard)))
        return 2
    result = compare(old, fp, strict=args.strict)
    if args.out:
        _save(args.out, result)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    if not args.quiet:
        _render(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
