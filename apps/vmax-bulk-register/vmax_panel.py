# -*- coding: utf-8 -*-
"""Panel-backed bulk registration: buy a number, register, harvest the token.

Adapters
--------
sms-activate  https://sms-activate.org  (action=getNumber / getStatus / setStatus)
5sim          https://5sim.net          (Bearer token, /v1/user/buy/activation/...)

Both panels hand you the phone number, so the flow is inverted compared with
``vmax_batch.py`` (which takes a CSV of numbers you already own):

    acquire number -> send-code -> poll panel for the SMS -> /register -> tokens

Usage
-----
    python vmax_panel.py --provider sms-activate --key $KEY --country china \
        --service <service-code> --count 5 --queue-dir tokens

    python vmax_panel.py --provider 5sim --key $KEY --country china \
        --product <product-code> --count 1 --dry-run

    python vmax_panel.py --provider sms-activate --key $KEY --list-countries

``--dry-run`` swaps the panel for an in-process mock so the whole loop
(captcha queue -> send-code -> poll -> register -> token) can be exercised
without credit; it still needs a real token in the queue and a real number.

Price guard: ``--max-price`` refuses to buy above the quoted price.

Form token
----------
``--form-captcha device`` walks the site's own signup-check path (R140): ``GET /login``
sets ``auth_device``, a random 32-hex ``auth_fingerprint`` follows, and ``POST /register``
then accepts an empty ``captcha_verify_param``.  ``/register/send-code`` still needs a
real token, so a card costs **1 token + 1 SMS** instead of 2.  A refusal at any step
falls back to a paid token (``form_captcha=device_fallback``) -- one wasted token is
cheaper than one wasted purchase.  The default stays ``auto`` so an existing command
line keeps its old meaning; the policy and how many cards actually rode the device
path are recorded in ``<out>.summary.json`` (``form_captcha`` / ``form_device`` /
``form_fallback``).

Bulk semantics (same colour of money as ``vmax_go``)
----------------------------------------------------
``--count N`` is a *target*, not "buy N more": accounts already ``status=ok``
in ``--out`` count toward it, so a crashed run resumes without over-buying.
Every row is appended the moment it is known (a crash cannot lose an account
already paid for), and ``<out>.summary.json`` records what was bought, what it
cost and why the run stopped.  ``--panel-recheck`` / ``--panel-recheck-seconds``
probe the panel for free (``balance()``) and stop *before* the next purchase.

Exit codes: 0 target met, 1 some accounts failed, 3 aborted on 429,
4 nothing to do, 5 ``--max-fail``, 6 ``--max-minutes``, 7 every number the
panel sold was already registered (bad pool), 9 panel down (acquire failed or
the probe did) -- 9 spends nothing from the point it is detected.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vmax_formcost as formcost  # noqa: E402
import vmax_sms  # noqa: E402
from vmax_client import VmaxError  # noqa: E402
from vmax_register import (classify_register_error, register_one,  # noqa: E402
                           RegisterError)
from vmax_sms import SmsProvider, SmsTimeout  # noqa: E402

CODE_RE = vmax_sms.CODE_RE


def _post(url: str, data: dict | None = None, headers: dict | None = None,
          proxy: str | None = None, timeout: int = 30):
    import requests

    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.post(url, data=data or {}, headers=headers or {}, timeout=timeout,
                      proxies=proxies)
    r.raise_for_status()
    return r


def _get(url: str, headers: dict | None = None, proxy: str | None = None,
         timeout: int = 30):
    import requests

    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.get(url, headers=headers or {}, timeout=timeout, proxies=proxies)
    r.raise_for_status()
    return r


class PanelError(RuntimeError):
    pass


class Panel:
    name = "base"

    def balance(self) -> float:  # pragma: no cover
        raise NotImplementedError

    def countries(self) -> dict:
        raise NotImplementedError

    def services(self) -> dict:
        raise NotImplementedError

    def acquire(self, service: str, country: str, max_price: float | None = None) -> dict:
        """Return {"handle": str, "phone": str, "price": float|None}."""
        raise NotImplementedError

    def code(self, handle: str) -> tuple[str, str]:
        """Return (state, value); state in {wait, retry, resend, cancel, ok}."""
        raise NotImplementedError

    def done(self, handle: str) -> None:
        pass

    def cancel(self, handle: str) -> None:
        pass


# ---------------------------------------------------------------- sms-activate
class SmsActivate(Panel):
    name = "sms-activate"
    BASE = "https://api.sms-activate.org/stubs/handler_api.php"

    def __init__(self, key: str, proxy: str | None = None):
        if not key:
            raise PanelError("sms-activate needs --key (api key)")
        self.key = key
        self.proxy = proxy

    def _call(self, action: str, **params):
        data = {"api_key": self.key, "action": action}
        data.update({k: v for k, v in params.items() if v is not None})
        r = _post(self.BASE, data=data, proxy=self.proxy)
        text = (r.text or "").strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except ValueError:
                return text
        return text

    def balance(self) -> float:
        out = self._call("getBalance")
        if isinstance(out, str) and out.startswith("ACCESS_BALANCE:"):
            return float(out.split(":", 1)[1])
        raise PanelError(f"getBalance failed: {out}")

    def countries(self) -> dict:
        out = self._call("getCountries")
        if not isinstance(out, dict):
            raise PanelError(f"getCountries failed: {out}")
        return out

    def services(self) -> dict:
        out = self._call("getServicesList")
        if isinstance(out, dict) and "services" in out:
            out = out["services"]
        if not isinstance(out, dict):
            raise PanelError(f"getServicesList failed: {str(out)[:200]}")
        return out

    def _resolve_country(self, country: str) -> str:
        country = str(country).strip()
        if country.isdigit():
            return country
        want = country.lower()
        for cid, meta in self.countries().items():
            name = str(meta.get("eng", meta.get("rus", ""))).lower()
            if name == want:
                return cid
        for cid, meta in self.countries().items():
            name = str(meta.get("eng", "")).lower()
            if want in name:
                return cid
        raise PanelError(f"country {country!r} not offered by sms-activate")

    def acquire(self, service: str, country: str, max_price: float | None = None) -> dict:
        cid = self._resolve_country(country)
        out = self._call("getNumberV2", service=service, country=cid)
        if isinstance(out, dict) and out.get("phoneNumber"):
            price = out.get("activationCost")
            price = float(price) if price not in (None, "") else None
            if max_price is not None and price is not None and price > max_price:
                self.cancel(str(out.get("activationId")))
                raise PanelError(f"price {price} above --max-price {max_price}")
            return {"handle": str(out.get("activationId")),
                    "phone": str(out.get("phoneNumber")),
                    "price": price}
        # legacy string form: ACCESS_NUMBER:<id>:<phone>
        if isinstance(out, str) and out.startswith("ACCESS_NUMBER:"):
            _, hid, phone = out.split(":", 2)
            return {"handle": hid, "phone": phone, "price": None}
        raise PanelError(f"getNumber failed: {out}")

    def code(self, handle: str) -> tuple[str, str]:
        out = self._call("getStatus", id=handle)
        if isinstance(out, str):
            if out == "STATUS_WAIT_CODE":
                return "wait", ""
            if out == "STATUS_WAIT_RETRY":
                return "retry", ""
            if out == "STATUS_WAIT_RESEND":
                return "resend", ""
            if out == "STATUS_CANCEL":
                return "cancel", ""
            if out.startswith("STATUS_OK:"):
                return "ok", out.split(":", 1)[1].strip()
        return "wait", ""

    def done(self, handle: str) -> None:
        self._call("setStatus", id=handle, status=6)

    def cancel(self, handle: str) -> None:
        self._call("setStatus", id=handle, status=8)

    def retry(self, handle: str) -> None:
        self._call("setStatus", id=handle, status=3)


# ----------------------------------------------------------------------- 5sim
class FiveSim(Panel):
    name = "5sim"
    BASE = "https://5sim.net/v1"

    def __init__(self, key: str, proxy: str | None = None):
        if not key:
            raise PanelError("5sim needs --key (bearer token)")
        self.key = key
        self.proxy = proxy
        self.headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}

    def balance(self) -> float:
        return float(_get(self.BASE + "/user/profile", self.headers, self.proxy).json()["balance"])

    def countries(self) -> dict:
        return _get(self.BASE + "/guest/countries", self.headers, self.proxy).json()

    def services(self) -> dict:
        return _get(self.BASE + "/guest/products/byCountry/china", self.headers,
                    self.proxy).json()

    def acquire(self, service: str, country: str, max_price: float | None = None) -> dict:
        cc = str(country).strip().lower().replace(" ", "")
        url = f"{self.BASE}/user/buy/activation/{cc}/any/{service}"
        r = _get(url, self.headers, self.proxy)
        if r.status_code != 200:
            raise PanelError(f"buy failed {r.status_code}: {r.text[:200]}")
        out = r.json()
        price = out.get("price")
        price = float(price) if price is not None else None
        if max_price is not None and price is not None and price > max_price:
            self.cancel(str(out.get("id")))
            raise PanelError(f"price {price} above --max-price {max_price}")
        return {"handle": str(out.get("id")), "phone": str(out.get("phone")), "price": price}

    def code(self, handle: str) -> tuple[str, str]:
        out = _get(f"{self.BASE}/user/check/{handle}", self.headers, self.proxy).json()
        sms = out.get("sms") or []
        for item in sms:
            m = CODE_RE.search(str(item.get("code") or item.get("text") or ""))
            if m:
                return "ok", m.group(1)
        return "wait", ""

    def done(self, handle: str) -> None:
        _get(f"{self.BASE}/user/finish/{handle}", self.headers, self.proxy)

    def cancel(self, handle: str) -> None:
        _get(f"{self.BASE}/user/cancel/{handle}", self.headers, self.proxy)


class MockPanel(Panel):
    """Dry-run panel: no purchase, code supplied by --dry-code or stdin."""

    name = "mock"

    def __init__(self, phone: str, code: str | None = None):
        self.phone = phone
        self.code_value = code
        self.used = []

    def balance(self) -> float:
        return 0.0

    def countries(self) -> dict:
        return {}

    def services(self) -> dict:
        return {}

    def acquire(self, service: str, country: str, max_price: float | None = None) -> dict:
        self.used.append(service)
        return {"handle": "mock-1", "phone": self.phone, "price": 0.0}

    def code(self, handle: str) -> tuple[str, str]:
        if self.code_value:
            return "ok", self.code_value
        return "wait", ""


class PanelSms(SmsProvider):
    """Bridge a Panel handle into the vmax_register SMS hook."""

    name = "panel"

    def __init__(self, panel: Panel, handle: str, interval: float = 5.0):
        self.panel = panel
        self.handle = handle
        self.interval = interval

    def wait(self, phone: str, timeout: int = 180, since: float | None = None) -> str:
        # `since` is honoured by HTTP/file providers; a panel activation is
        # already scoped to this phone, so nothing to filter here.
        deadline = time.time() + timeout
        last = "wait"
        while time.time() < deadline:
            state, value = self.panel.code(self.handle)
            last = state
            if state == "ok" and value:
                return value
            if state == "cancel":
                raise SmsTimeout(f"panel cancelled activation for {phone}")
            if state == "retry":
                try:
                    self.panel.retry(self.handle)
                except Exception:  # noqa: BLE001 - best effort
                    pass
            time.sleep(self.interval)
        raise SmsTimeout(f"panel returned no code for {phone} in {timeout}s (last={last})")


_CURRENT_PANEL = [None]
vmax_sms.register_provider("panel", lambda spec: PanelSms(_CURRENT_PANEL[0], spec))  # type: ignore[index]


def _register_one_args(ns, phone: str, handle: str):
    a = argparse.Namespace(**vars(ns))
    a.phone = phone
    a.sms = "panel:" + handle
    return a


def _load_done(out: str | None) -> tuple[set[str], int]:
    """Phones already ``status=ok`` in *out* (the resume target) and rows seen."""
    done: set[str] = set()
    rows = 0
    if not out or not os.path.exists(out):
        return done, rows
    with open(out, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rows += 1
            if rec.get("status") == "ok" and rec.get("phone"):
                done.add(str(rec["phone"]))
    return done, rows


def _append(out: str | None, rec: dict) -> None:
    """Append one row *now*: a crash must not lose an account already paid for."""
    if not out:
        return
    d = os.path.dirname(os.path.abspath(out))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_summary(out: str | None, summary: dict) -> str | None:
    if not out:
        return None
    sp = out + ".summary.json"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    return sp


def _register_row(ns, phone: str, handle: str, register) -> dict:
    """Run one registration and never raise: a failure comes back as a row.

    Same contract as ``vmax_go.run_one``: ``RegisterError`` keeps its classified
    bucket (``phone_taken`` / ``rate_limited`` / ``code_wrong`` ...) plus the
    partial row, so one bad number cannot kill a run that already paid for the
    numbers before it.
    """
    ns_one = _register_one_args(ns, phone, handle)
    try:
        return register(ns_one)
    except RegisterError as e:
        res = dict(e.result)
        res.update({"phone": phone, "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                    "register_error_kind": e.kind, "ts": int(time.time())})
        return res
    except Exception as e:  # noqa: BLE001 - one bad number must not kill the run
        return {"phone": phone, "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "register_error_kind": classify_register_error(str(e)),
                "ts": int(time.time())}


def _panel_probe(panel: Panel) -> tuple[bool, str]:
    """Free health check (``balance()``); a verdict, never an exception."""
    try:
        return True, "balance=%s" % (panel.balance(),)
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, str(e)[:160])


def panel_exit(summary: dict) -> int:
    if summary.get("aborted_on_429"):
        return 3
    if summary.get("stopped_on_panel_down"):
        return 9
    if summary.get("stopped_on_max_fail"):
        return 5
    if summary.get("stopped_on_max_minutes"):
        return 6
    if summary.get("taken") and not summary.get("ok") and not summary.get("fail"):
        # Every number the panel sold was already registered: the pool is bad,
        # which is a different message from "registration failed" (exit 1).
        return 7
    if not summary.get("remaining_at_start"):
        return 4
    if summary.get("target_met"):
        return 0
    return 1


def run(ns, register=None) -> dict:
    """Buy -> register -> append, one number at a time.

    Stop rules mirror ``vmax_go`` because the money is the same colour:

    * ``--count N`` is a *target*: numbers already ``status=ok`` in ``--out``
      count toward it, so a crashed run resumes instead of over-buying;
    * 429 aborts immediately (do not buy the next number into a ban);
    * ``--max-fail`` / ``--max-minutes`` stop the run;
    * every ``--panel-recheck`` purchases (or ``--panel-recheck-seconds``) the
      panel is probed for free; a failed probe stops *before* the next purchase.
    """
    panel = ns._panel
    _CURRENT_PANEL[0] = panel
    register = register or register_one
    out = getattr(ns, "out", None)
    done, prior_rows = _load_done(out)
    target = max(0, int(getattr(ns, "count", 1) or 0))
    remaining = max(0, target - len(done))
    results: list[dict] = []
    ok = fail = taken = 0
    bought = probes = dupes = 0
    reasons: dict[str, int] = {}
    panel_down = None
    aborted = hit_max_fail = hit_minutes = False
    started = time.time()
    seen: set[str] = set()
    last_probe = time.time()

    print("[panel] target %d account(s); %d already ok in %s -> %d to buy"
          % (target, len(done), out or "(no --out)", remaining), flush=True)
    form_policy = getattr(ns, "form_captcha", "auto") or "auto"
    print("[panel] form captcha: %s%s" % (
        form_policy,
        " (POST /register takes an empty token from a trusted device; send-code still "
        "costs 1)" if form_policy == "device" else ""), flush=True)
    if remaining == 0:
        print("[panel] nothing to do (target already met) -- 0 purchase", flush=True)

    for i in range(remaining):
        if getattr(ns, "max_minutes", 0) and (time.time() - started) / 60.0 >= ns.max_minutes:
            hit_minutes = True
            print("[panel] --max-minutes reached; stopping", flush=True)
            break
        if getattr(ns, "max_fail", 0) and fail >= ns.max_fail:
            hit_max_fail = True
            print("[panel] --max-fail %d reached; stopping" % ns.max_fail, flush=True)
            break

        every_n = getattr(ns, "panel_recheck", 5) or 0
        every_s = getattr(ns, "panel_recheck_seconds", 300.0) or 0.0
        if bought and ((every_n and bought % every_n == 0)
                       or (every_s and time.time() - last_probe >= every_s)):
            probes += 1
            last_probe = time.time()
            good, detail = _panel_probe(panel)
            print("[panel] probe: %s -- %s" % ("OK" if good else "DOWN", detail), flush=True)
            if not good:
                panel_down = detail
                print("[panel] stopping before the next purchase (0 spent from here)",
                      flush=True)
                break

        try:
            acq = panel.acquire(ns.service, ns.country, ns.max_price)
        except Exception as e:  # noqa: BLE001 - cannot buy == channel down
            panel_down = "%s: %s" % (type(e).__name__, str(e)[:160])
            print("[panel] acquire failed (%s) -> stopping, 0 more purchase" % panel_down,
                  flush=True)
            break
        bought += 1
        handle, phone = str(acq.get("handle")), str(acq.get("phone"))
        price = acq.get("price")

        if phone in seen or phone in done:
            dupes += 1
            print("[panel] %s was already used -> cancelled, not registered twice" % phone,
                  flush=True)
            try:
                panel.cancel(handle)
            except Exception:  # noqa: BLE001 - best effort
                pass
            continue
        seen.add(phone)
        print("[panel] %d/%d %s (id=%s, price=%s)"
              % (i + 1, remaining, phone, handle, price), flush=True)

        res = _register_row(ns, phone, handle, register)
        res["panel"] = panel.name
        res["panel_price"] = price
        res["panel_handle"] = handle
        _append(out, res)
        results.append(res)

        kind = res.get("register_error_kind")
        if res.get("status") == "ok":
            ok += 1
            print("[panel] %s ok uid=%s" % (phone, res.get("uid")), flush=True)
        elif kind == "phone_taken":
            taken += 1
            reasons[kind] = reasons.get(kind, 0) + 1
            print("[panel] %s already registered -> money lost, cancelled" % phone, flush=True)
        else:
            fail += 1
            reasons[kind or "error"] = reasons.get(kind or "error", 0) + 1
            print("[panel] %s %s: %s"
                  % (phone, kind or "error", str(res.get("error"))[:80]), flush=True)

        try:
            if res.get("status") == "ok":
                panel.done(handle)
            else:
                panel.cancel(handle)
        except Exception as e:  # noqa: BLE001 - best effort, the row is already saved
            print("[panel] could not close activation %s: %s" % (handle, e), flush=True)

        if kind == "rate_limited" or "429" in str(res.get("error", "")):
            aborted = True
            print("[panel] 429 -> stopping (do not buy more numbers into a ban)", flush=True)
            break
        if len(done) + ok >= target:
            break
        if getattr(ns, "delay", 0) and i + 1 < remaining:
            time.sleep(ns.delay)

    summary = {
        "tool": "vmax_panel", "ts": int(time.time()),
        "elapsed_s": round(time.time() - started, 1),
        "provider": panel.name,
        "service": getattr(ns, "service", ""), "country": getattr(ns, "country", ""),
        "out": os.path.abspath(out) if out else None,
        "target": target, "prior_ok": len(done), "prior_rows": prior_rows,
        "remaining_at_start": remaining,
        "bought": bought, "ok": ok, "fail": fail, "taken": taken,
        "panel_dupes": dupes, "probes": probes,
        "aborted_on_429": aborted, "stopped_on_max_fail": hit_max_fail,
        "stopped_on_max_minutes": hit_minutes,
        "stopped_on_panel_down": bool(panel_down), "panel_down": panel_down,
        "error_kinds": reasons, "target_met": len(done) + ok >= target,
        # Money audit: the policy is what was asked for, the counters are what
        # actually happened (a device refusal silently degrades a card to 2 tokens).
        # One shared price list (R152) -- this file keeps no bucket names of its own.
        "form_captcha": form_policy,
        **formcost.summarize(formcost.count_of(results)),
    }
    code = panel_exit(summary)
    summary["exit_code"] = code
    sp = _write_summary(out, summary)
    if sp:
        print("[panel] summary -> %s" % sp, flush=True)
    return {"results": results, "summary": summary, "exit": code}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Panel-backed vobilemax bulk registration")
    ap.add_argument("--provider", default="sms-activate", choices=["sms-activate", "5sim", "mock"])
    ap.add_argument("--key", default=os.environ.get("SMS_PANEL_KEY", ""))
    ap.add_argument("--service", default="", help="panel service code (see --list-services)")
    ap.add_argument("--product", default="", help="5sim product code (defaults to --service)")
    ap.add_argument("--country", default="china")
    ap.add_argument("--count", type=int, default=1,
                    help="target number of accounts; rows already ok in --out count too")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between accounts")
    ap.add_argument("--max-price", type=float, default=None)
    ap.add_argument("--max-fail", type=int, default=0,
                    help="stop after N failed accounts (0 = only stop on 429/panel down)")
    ap.add_argument("--max-minutes", type=float, default=0)
    ap.add_argument("--panel-recheck", type=int, default=5,
                    help="probe the panel every N purchases (0 = off)")
    ap.add_argument("--panel-recheck-seconds", type=float, default=300.0,
                    help="... or every S seconds (0 = off)")
    ap.add_argument("--list-countries", action="store_true")
    ap.add_argument("--list-services", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="use MockPanel (no purchase)")
    ap.add_argument("--dry-phone", default="", help="phone for --dry-run")
    ap.add_argument("--dry-code", default="", help="SMS code for --dry-run (omit to poll stdin)")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--out", default=None,
                    help="results.jsonl (appended per account; also the resume target)")
    # passthrough options for register_one
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--invite", default="")
    ap.add_argument("--captcha", default="queue", choices=["auto", "manual", "assist", "param", "queue"])
    ap.add_argument("--captcha-param", default="")
    ap.add_argument("--queue-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=0)
    ap.add_argument("--captcha-attempts", type=int, default=6)
    ap.add_argument("--form-captcha", default="auto",
                    choices=["auto", "reuse", "fresh", "device"],
                    help="captcha for POST /register: device = the site's own "
                         "signup-check path (empty form token, 1 token/card); "
                         "auto/reuse/fresh = the token is also spent on the form")
    ap.add_argument("--assist-dir", default="assist")
    ap.add_argument("--allow-virtual", action="store_true")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-timeout", type=int, default=180)
    ap.add_argument("--sms-resends", type=int, default=1,
                    help="re-send after the 60s cooldown when the code is rejected")
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--debug-dir", default=None)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    ns = ap.parse_args(argv)

    if ns.dry_run:
        if not ns.dry_phone:
            ap.error("--dry-run needs --dry-phone")
        ns._panel = MockPanel(ns.dry_phone, ns.dry_code or None)
    elif ns.provider == "sms-activate":
        ns._panel = SmsActivate(ns.key, ns.proxy)
    else:
        ns._panel = FiveSim(ns.key, ns.proxy)
    panel = ns._panel

    if ns.list_countries:
        print(json.dumps(panel.countries(), ensure_ascii=False, indent=1)[:4000])
        return 0
    if ns.list_services:
        print(json.dumps(panel.services(), ensure_ascii=False, indent=1)[:4000])
        return 0

    ns.service = ns.service or ns.product or "vobilemax"
    if panel.name == "5sim" and ns.product:
        ns.service = ns.product

    out = run(ns)
    s = out["summary"]
    print("[panel] done: %d/%d bought, %d ok, %d fail, %d taken -> exit %d"
          % (s["bought"], s["target"], s["ok"], s["fail"], s["taken"], out["exit"]),
          flush=True)
    return out["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
