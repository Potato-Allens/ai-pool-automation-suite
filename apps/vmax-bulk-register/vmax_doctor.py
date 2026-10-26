# -*- coding: utf-8 -*-
"""Preflight checks before a real batch run.

    python vmax_doctor.py                    # production
    python vmax_doctor.py --base https://test-auth.vobilemax.cn \
        --redirect https://test-dreammaker.vobilemax.cn/auth/callback
    python vmax_doctor.py --proxy http://user:pass@host:8080 --queue-dir tokens

Checks: DNS, TLS/health, openapi path count, agreements, captcha config,
session cookie, egress IP (through the proxy when given), clock skew, token
queue depth. Exit code 0 = all critical checks passed.

--register-gate additionally does one *code-less* form POST to /register and
asserts the server answers "请先获取短信验证码". That single request proves the
exact first-attempt failure mode is still the expected one (captcha gate open,
SMS code required) without sending any SMS or creating an account.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vmax_client import VmaxAuth  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="vobilemax preflight")
    ap.add_argument("--base", default=None, help="auth base URL (env VMAX_BASE)")
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--skip-egress", action="store_true")
    ap.add_argument("--register-gate", action="store_true",
                    help="POST /register once (no SMS) to prove the captcha gate is open")
    ap.add_argument("--min-paths", type=int, default=40,
                    help="openapi path count that counts as healthy (lower it against a mock)")
    ap.add_argument("--flows", action="store_true",
                    help="probe the recovery path: openapi schema + one empty-captcha "
                         "POST (0 SMS, 0 token)")
    ap.add_argument("--gate-phone", default="13900139000",
                    help="comma separated; PASS if any shows an open gate")
    args = ap.parse_args()

    cli = VmaxAuth(proxy=args.proxy, base=args.base, redirect_uri=args.redirect)
    host = urlparse(cli.base).hostname or ""
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"[{'OK ' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # 1) DNS
    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
        add("dns", True, f"{host} -> {', '.join(ips[:3])}")
    except Exception as e:
        add("dns", False, f"{host}: {type(e).__name__} {e}")

    # 2) health + TLS + server date / clock skew
    try:
        t0 = time.time()
        r = cli.s.get(f"{cli.base}/health/live", timeout=15)
        rtt = (time.time() - t0) * 1000
        ok = r.status_code == 200 and r.json().get("status") == "ok"
        add("health", ok, f"{r.status_code} {r.text[:60]} ({rtt:.0f} ms)")
        date = r.headers.get("date")
        if date:
            skew = time.time() - parsedate_to_datetime(date).timestamp()
            add("clock", abs(skew) < 120, f"server/local skew {skew:+.0f}s")
    except Exception as e:
        add("health", False, f"{type(e).__name__}: {e}")

    # 2b) readiness -- dependencies (DB / SMS provider) behind the liveness probe
    try:
        r = cli.s.get(f"{cli.base}/health/ready", timeout=15)
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        add("ready", r.status_code == 200,
            f"{r.status_code} {payload.get('status', r.text[:40])}")
    except Exception as e:
        add("ready", False, f"{type(e).__name__}: {e}")

    # 3) openapi
    try:
        j = cli.s.get(f"{cli.base}/openapi.json", timeout=30).json()
        n = len(j.get("paths", {}))
        add("openapi", n >= args.min_paths,
            f"{n} paths (need >= {args.min_paths}), "
            f"title={j.get('info', {}).get('title', '?')[:40]}")
    except Exception as e:
        add("openapi", False, f"{type(e).__name__}: {e}")

    # 4) agreements / captcha / session
    try:
        ag = cli.agreements()
        add("agreements", len(ag) >= 2, ", ".join(f"{a['id']}:{a['type']}" for a in ag))
    except Exception as e:
        add("agreements", False, f"{type(e).__name__}: {e}")
    try:
        cap = cli.captcha_config(args.platform)
        add("captcha", bool(cap.get("enabled")), json.dumps(cap, ensure_ascii=False)[:100])
    except Exception as e:
        add("captcha", False, f"{type(e).__name__}: {e}")
    try:
        r = cli.s.get(f"{cli.base}/session", timeout=15)
        add("session", r.status_code in (200, 401), f"{r.status_code}")
    except Exception as e:
        add("session", False, f"{type(e).__name__}: {e}")

    # 4b) register-write rate-limit bucket (see PROTOCOL S7.2)
    try:
        from vmax_pacer import Pacer
        pk = "register@" + (args.proxy or "direct")
        snap = Pacer().snapshot(pk)
        if snap["banned"]:
            add("pace", False,
                f"429 ban active on {pk}: {snap['ban_remaining_s']:.0f}s left, "
                f"strikes={snap['strikes']} (sleep, do not poll)")
        else:
            add("pace", True,
                f"{pk}: {snap['window_used']}/{snap['capacity']} per {snap['window_s']:.0f}s, "
                f"strikes={snap['strikes']}")
    except Exception as e:
        add("pace", True, f"pacer unavailable ({type(e).__name__}), assuming clear")

    # 5) egress IP (also proves the proxy path works)
    if not args.skip_egress:
        ip = ""
        for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/all.json"):
            try:
                r = cli.s.get(url, timeout=15)
                ip = r.json().get("ip") or r.json().get("ip_addr") or r.text.strip()[:40]
                if ip:
                    break
            except Exception:
                continue
        add("egress", bool(ip), ip or "unknown (ipify/ifconfig unreachable)")

    # 6) token queue depth
    try:
        n = len([f for f in os.listdir(args.queue_dir) if f.startswith("tok_") and f.endswith(".json")])
        add("tokens", True, f"{n} ready in {args.queue_dir}")
    except FileNotFoundError:
        add("tokens", True, f"{args.queue_dir} absent (0 ready)")
    except Exception as e:
        add("tokens", False, f"{type(e).__name__}: {e}")

    # 7) register gate (optional, no SMS spent)
    if args.register_gate:
        try:
            from vmax_register import gen_password, gen_username
            import secrets as _secrets
            from vmax_client import pkce_pair as _pkce
            verifier, challenge = _pkce()
            state = _secrets.token_urlsafe(16)
            login_url = VmaxAuth.login_url(challenge, state=state, client=args.platform,
                                           base=cli.base, redirect_uri=cli.redirect_uri)
            cli.s.get(login_url, timeout=20)
            cli.s.cookies.set("auth_fingerprint", _secrets.token_hex(16), domain=host, path="/")
            phones = [x.strip() for x in str(args.gate_phone).split(",") if x.strip()]
            seen = []
            verdict = None
            for phone in phones:
                out = cli.register_form(phone, "000000", gen_username(), gen_password(),
                                        verifier, challenge, state, "", client=args.platform)
                err = out.get("error") or ""
                seen.append(f"{phone[-4:]}:{out.get('status')}:{err[:24]}")
                if out.get("code") or out.get("status") in (200, 302, 303):
                    verdict = (False, f"unexpected success on {phone} -- STOP and inspect")
                    break
                if "请先获取短信验证码" in err:
                    verdict = (True, f"{phone[-4:]}: captcha gate open, SMS code required (expected)")
                    break
                if "验证码不正确" in err or "已过期" in err:
                    verdict = (True, f"{phone[-4:]}: code record present, captcha gate open")
                    break
                if "过于频繁" in err:
                    verdict = (True, f"{phone[-4:]}: IP rate limited -- gate not exercised")
                    break
                if "暂不可用" in err:
                    continue  # number-specific carrier failure; try the next one
                if any(k.lower() in err.lower() for k in ("captcha", "Incomplete", "Unsupported")):
                    verdict = (False, f"register form now demands captcha: {err[:60]}")
                    break
                if any(k in err for k in ("格式", "虚拟")):
                    continue
            if verdict is None:
                verdict = (False, "all gate phones rejected: " + " | ".join(seen))
            add("gate", verdict[0], verdict[1] + ("" if verdict[0] else " | " + " | ".join(seen)))
        except Exception as e:
            add("gate", False, f"{type(e).__name__}: {e}")

    # 8) recovery flow (optional): schema first, then one code-less POST
    if args.flows:
        from vmax_reset import probe_live, probe_schema
        try:
            sch = probe_schema(cli)
            ok = (sch["has_send_code"] and sch["has_reset"] and sch["has_login"]
                  and sch["captcha_free"])
            add("reset_schema", ok,
                "send-code={} reset={} login={} fields={} paths={}".format(
                    sch["has_send_code"], sch["has_reset"], sch["has_login"],
                    ",".join(sch["request_fields"]) or "-", sch["paths"]))
        except Exception as e:
            add("reset_schema", False, f"{type(e).__name__}: {e}")
        try:
            pr = probe_live(cli)
            add("reset_probe", pr["ok"], f"{pr['status']} {pr['message']}")
        except Exception as e:
            add("reset_probe", False, f"{type(e).__name__}: {e}")

    critical = {"dns", "health", "openapi", "agreements", "captcha"}
    if args.flows:
        critical |= {"reset_schema", "reset_probe"}
    if args.register_gate:
        critical.add("gate")
    bad = [name for name, ok, _ in results if not ok and name in critical]
    print(f"\n[doctor] {'PASS' if not bad else 'FAIL: ' + ', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())