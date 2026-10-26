# -*- coding: utf-8 -*-
"""SMS code acquisition hooks for vmax_register / vmax_batch."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

CODE_RE = re.compile(r"\b(\d{6})\b")


_PROVIDERS = {}


def register_provider(prefix: str, factory):
    """Register a custom ``prefix:arg`` scheme for --sms (e.g. ``panel:<id>``)."""
    _PROVIDERS[prefix] = factory

class SmsTimeout(RuntimeError):
    pass


def since_param(since: float | None) -> str:
    """Render ``{since}`` with sub-second precision.

    ``str(int(...))`` truncates, so a code that arrived *before* the send but in
    the same wall-clock second still passed the "arrived at or after" filter.
    Real repro: vmax_probe then vmax_batch on the same number inside one second
    -> the old code came back and /register answered 验证码不正确.  Three
    decimals are enough to order the two events and are still plain unix
    seconds, so any panel that accepted ``1699999999`` accepts
    ``1699999999.123``; providers that insist on integers can use
    ``{since_int}``.
    """
    return f"{(since if since else time.time()):.3f}"


class SmsProvider:
    name = "base"

    def wait(self, phone: str, timeout: int = 180, since: float | None = None) -> str:
        """Return a 6-digit code for *phone*.

        ``since`` (unix seconds) means "only accept SMS that arrived at or after
        this moment"; providers that can honour it must, so a resent code can
        never be confused with the previous one.
        """
        raise NotImplementedError

    def probe(self, timeout: float = 6.0) -> tuple[bool, str]:
        """Is this channel able to deliver a code right now? (0 SMS, 0 token)

        Run before the farm starts: finding out that the receiver is down after
        paying a captcha token (and a real SMS) is the expensive way to learn it.
        The base implementation is permissive so custom ``prefix:`` providers
        keep working without implementing it.
        """
        return True, "no probe for %s" % self.name


class CommandSms(SmsProvider):
    """Run a shell command, e.g. `python get_sms.py --phone {phone}`.

    The command must print the 6 digit code somewhere on stdout.
    """

    name = "cmd"

    def __init__(self, command: str, interval: float = 3.0):
        self.command = command
        self.interval = interval

    def probe(self, timeout: float = 6.0) -> tuple[bool, str]:
        parts = self.command.split()
        first = parts[0] if parts else ""
        if first and (shutil.which(first) or os.path.exists(first)):
            return True, "command present: %s" % first
        return False, "command not found: %r" % first

    def wait(self, phone: str, timeout: int = 180, since: float | None = None) -> str:
        cmd = self.command.replace("{phone}", phone)
        cmd = cmd.replace("{since}", since_param(since))
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
                last = (out.stdout or "") + (out.stderr or "")
                m = CODE_RE.search(last)
                if m:
                    return m.group(1)
            except subprocess.TimeoutExpired:
                last = "command timeout"
            time.sleep(self.interval)
        raise SmsTimeout(f"no SMS code for {phone} within {timeout}s (last output: {last[-300:]})")


class FileSms(SmsProvider):
    """Watch <dir>/<phone>.txt (or <dir>/<phone>.json|.log) for a 6 digit code."""

    name = "file"

    def __init__(self, directory: str, interval: float = 1.0):
        self.directory = directory
        self.interval = interval

    def probe(self, timeout: float = 6.0) -> tuple[bool, str]:
        path = os.path.abspath(self.directory)
        if os.path.isdir(path):
            return True, "watching %s" % path
        return False, "sms dir does not exist: %s (create it, or fix --sms-dir)" % path

    def wait(self, phone: str, timeout: int = 180, since: float | None = None) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for ext in (".txt", ".json", ".log", ".sms"):
                path = os.path.join(self.directory, phone + ext)
                if os.path.exists(path):
                    if since and os.path.getmtime(path) < since:
                        continue
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                            text = f.read()
                    except OSError:
                        continue
                    m = CODE_RE.search(text)
                    if m:
                        return m.group(1)
            time.sleep(self.interval)
        raise SmsTimeout(f"no SMS file for {phone} within {timeout}s in {self.directory}")


class HttpSms(SmsProvider):
    """Poll an HTTP JSON/text endpoint, e.g. `https://sms.example/api?phone={phone}`.

    Looks for a 6 digit code in common JSON keys (code / sms_code / message)
    or anywhere in the body.
    """

    name = "http"

    def __init__(self, url: str, interval: float = 3.0):
        self.url = url
        self.interval = interval

    @staticmethod
    def _dig(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in ("code", "sms_code", "smscode", "verify_code", "captcha"):
                    m = CODE_RE.search(str(v))
                    if m:
                        return m.group(1)
            for v in obj.values():
                got = HttpSms._dig(v)
                if got:
                    return got
        elif isinstance(obj, list):
            for v in obj:
                got = HttpSms._dig(v)
                if got:
                    return got
        return None

    @staticmethod
    def request_timeout(url: str, default: float = 15.0) -> float:
        """Socket timeout that matches the endpoint's own long-poll window.

        ``vmax_smsd`` blocks up to ``?timeout=NN`` before answering, so a fixed
        15 s socket timeout aborts healthy long-polls and leaves a server thread
        behind on every retry.  Honour the advertised window (capped at 310 s).
        """
        import re as _re

        m = _re.search(r"[?&]timeout=(\d+(?:\.\d+)?)", url or "")
        if not m:
            return default
        try:
            return min(float(m.group(1)) + 5.0, 310.0)
        except ValueError:
            return default

    def probe(self, timeout: float = 6.0) -> tuple[bool, str]:
        """Check ``/health`` (falling back to a sentinel fetch on older relays)."""
        import requests
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(self.url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        path = parts.path
        if path.rstrip("/").endswith("/code"):
            path = path.rstrip("/")[: -len("/code")] + "/health"
        keep = {k: v for k, v in query.items() if k in ("secret", "token", "key", "password")}
        health = urlunsplit((parts.scheme, parts.netloc, path, urlencode(keep), ""))
        try:
            r = requests.get(health, timeout=min(timeout, 5.0))
        except Exception as exc:  # noqa: BLE001 - this is the answer we want to report
            return False, "unreachable %s: %s: %s" % (parts.netloc, type(exc).__name__, exc)
        if r.status_code == 401:
            return False, "secret rejected (401) by %s" % parts.netloc
        if r.status_code == 200:
            detail = "health 200"
            try:
                body = r.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                modems = body.get("modems")
                if isinstance(modems, list) and modems:
                    detail += "; modems=" + ",".join(
                        "%s:%s" % (m.get("name"), "up" if m.get("connected") else "down")
                        for m in modems if isinstance(m, dict))
                    if not any(isinstance(m, dict) and m.get("connected") for m in modems):
                        return False, detail + " (no modem connected - check --modem / USB)"
                if isinstance(body.get("stored"), int):
                    detail += "; stored=%d" % body["stored"]
            return True, detail
        # Older relay without /health: prove reachability + auth with a sentinel number.
        fallback = self.url.replace("{phone}", "19900000000").replace("{since}", "0")
        fallback = fallback.replace("{since_int}", "0")
        fp = urlsplit(fallback)
        fq = dict(parse_qsl(fp.query, keep_blank_values=True))
        fq["phone"] = "19900000000"
        fq["timeout"] = "0"
        fq.pop("since", None)
        sentinel = urlunsplit((fp.scheme, fp.netloc, fp.path, urlencode(fq), ""))
        try:
            r2 = requests.get(sentinel, timeout=min(timeout, 5.0))
        except Exception as exc:  # noqa: BLE001
            return False, "no /health and sentinel fetch failed: %s: %s" % (
                type(exc).__name__, exc)
        if r2.status_code == 401:
            return False, "secret rejected (401)"
        return True, "reachable (no /health; sentinel -> %d)" % r2.status_code

    def wait(self, phone: str, timeout: int = 180, since: float | None = None) -> str:
        import requests

        url = self.url.replace("{phone}", phone)
        url = url.replace("{since}", since_param(since))
        url = url.replace("{since_int}", str(int(since if since else time.time())))
        req_timeout = self.request_timeout(url)
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                r = requests.get(url, timeout=req_timeout)
                last = r.text
                try:
                    got = self._dig(r.json())
                except ValueError:
                    got = None
                if got:
                    return got
                m = CODE_RE.search(last)
                if m:
                    return m.group(1)
            except Exception as e:  # noqa: BLE001 - keep polling
                last = f"{type(e).__name__}: {e}"
            time.sleep(self.interval)
        raise SmsTimeout(f"no SMS code for {phone} within {timeout}s from {url} (last: {last[-200:]})")


class PromptSms(SmsProvider):
    name = "prompt"

    def probe(self, timeout: float = 6.0) -> tuple[bool, str]:
        return True, "manual entry (prompt)"

    def wait(self, phone: str, timeout: int = 600, since: float | None = None) -> str:
        while True:
            code = input(f"[sms] code for {phone}: ").strip()
            if CODE_RE.fullmatch(code):
                return code
            print("[sms] expected 6 digits")


class NullSms(SmsProvider):
    name = "none"

    def probe(self, timeout: float = 6.0) -> tuple[bool, str]:
        return False, "the `none` provider never delivers a code (use cmd/file/http)"

    def wait(self, phone: str, timeout: int = 1, since: float | None = None) -> str:
        raise SmsTimeout("no SMS provider configured (--sms cmd|file|prompt)")


def build_sms(spec: str, directory: str = "sms_inbox") -> SmsProvider:
    spec = (spec or "none").strip()
    # accept both "http:http://host/path" (documented form) and a bare URL
    for pre in ("http:", "https:"):
        if spec.startswith(pre) and spec[len(pre):].startswith(("http://", "https://")):
            spec = spec[len(pre):]
            break
    if spec.startswith("cmd:"):
        return CommandSms(spec[4:])
    if spec.startswith("http://") or spec.startswith("https://"):
        return HttpSms(spec)
    if spec.startswith("file"):
        return FileSms(directory)
    if spec.startswith("panel:"):
        try:
            import vmax_panel  # noqa: F401  (registers the "panel" provider)
        except Exception:  # noqa: BLE001 - fall through to a clear error
            pass
    for prefix, factory in _PROVIDERS.items():
        if spec.startswith(prefix + ":"):
            return factory(spec[len(prefix) + 1:])
    if spec.startswith("panel:"):
        raise SmsTimeout("panel: source needs vmax_panel.py next to vmax_sms.py")
    if spec == "prompt":
        return PromptSms()
    return NullSms()


def channel_check(spec: str, directory: str = "sms_inbox",
                  timeout: float = 6.0) -> dict:
    """Probe an ``--sms`` spec without spending a token or a single SMS.

    One vocabulary for every entry point (day / go / batch / CLI) so that "down"
    always means the same thing.  Never raises: a spec that cannot even be built
    is reported as down, with the exception text as the detail.
    """
    provider = None
    try:
        provider = build_sms(spec, directory)
        ok, detail = provider.probe(timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - this *is* the verdict
        ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
    return {"ok": bool(ok), "detail": detail,
            "provider": type(provider).__name__ if provider is not None else None}


def main(argv=None) -> int:
    """`python vmax_sms.py --probe "http:http://127.0.0.1:8787/code?phone={phone}&secret=..."`

    Costs 0 token and 0 SMS.  Exit 0 = the channel can deliver, 1 = it cannot.
    """
    import argparse
    import sys

    try:
        from vmax_console import utf8_stdout
        utf8_stdout()
    except Exception:  # pragma: no cover
        pass

    ap = argparse.ArgumentParser(description="check an --sms channel before spending tokens")
    ap.add_argument("--probe", required=True, metavar="SPEC", help="same value as --sms")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ns = ap.parse_args(argv)
    chk = channel_check(ns.probe, ns.sms_dir)
    ok, detail = chk["ok"], chk["detail"]
    print(("OK   " if ok else "DOWN ") + detail)
    if not ok:
        print("hint: check the daemon / modem, the secret, and --sms-dir", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

