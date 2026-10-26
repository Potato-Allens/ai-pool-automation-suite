# -*- coding: utf-8 -*-
"""Single account: captcha -> SMS -> register -> OAuth code -> JWT.

Examples
--------
Manual captcha, console SMS:
    python vmax_register.py --phone 13800000000 --sms prompt --captcha manual

Auto captcha, SMS pulled from a command:
    python vmax_register.py --phone 13800000000 --sms "cmd:python get_sms.py --phone {phone}"

Reuse a previously solved captcha token:
    python vmax_register.py --phone 13800000000 --captcha param --captcha-param eyJ...
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import json
import re
import os
import random
import secrets
import string
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vmax_console import utf8_stdout  # noqa: E402

utf8_stdout()


from vmax_client import CLIENTS, VmaxAuth, VmaxError, pkce_pair  # noqa: E402
from vmax_sms import SmsTimeout, build_sms  # noqa: E402
import vmax_journal as journal  # noqa: E402


class RegisterError(VmaxError):
    """A classified registration failure that still carries the partial row.

    ``kind`` uses the vocabulary of :func:`classify_register_error`
    (``phone_taken``, ``rate_limited``, ``code_wrong``, ...).  The partial
    ``result`` is what the batch runners append to results.jsonl, so a run that
    stops on 429 -- or skips a number that is already registered -- keeps the
    evidence it has already paid for (captcha timing, send-code answer, HTTP
    status) instead of a bare string.
    """

    def __init__(self, message: str, kind: str = "other", result: dict | None = None,
                 status: int | None = None, payload=None):
        super().__init__(message, status, payload)
        self.kind = kind
        self.result = result or {}


def _reject(message: str, kind: str, result: dict, status: int | None = None,
            payload=None):
    """Raise a classified failure, stamping the bucket into the row itself."""
    row = dict(result)
    row["register_error_kind"] = kind
    if status is not None:
        row["register_http_status"] = status
    raise RegisterError(message, kind=kind, result=row, status=status, payload=payload)


def gen_password(n: int = 14) -> str:
    """Password policy observed: >=8 chars, upper + lower + digit."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n - 3))
        pw = secrets.choice(string.ascii_uppercase) + secrets.choice(string.ascii_lowercase) + \
            secrets.choice(string.digits) + pw
        pw = "".join(random.sample(pw, len(pw)))
        if (any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def gen_username(prefix: str = "u") -> str:
    return prefix + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))


def _poplog(rec: dict) -> None:
    """Opt-in claim trace (VMAX_POP_LOG=path). One JSONL line per claim; never raises."""
    path = os.environ.get("VMAX_POP_LOG")
    if not path:
        return
    try:
        rec = dict(rec, ts=round(time.time(), 3), pid=os.getpid(),
                   tid=threading.get_ident())
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - a debug hook must never break a run
        pass


@contextlib.contextmanager
def _queue_lock(queue_dir: str, stale_after: float = 20.0, timeout: float = 30.0):
    """Cross-process mutex for the token queue (an atomically created directory).

    Renaming a file is NOT a usable claim primitive on this Windows box: under an
    8-thread load the same source could be renamed 2-8 times (the first rename is
    not always committed before the next one lands), which handed the same token
    to several workers.  ``os.mkdir`` has no such window: exactly one caller wins.

    A holder that dies leaves the directory behind, so a lock older than
    ``stale_after`` seconds is taken over; a pop takes microseconds, so this only
    ever fires after a crash.
    """
    path = os.path.join(queue_dir, ".pop.lock")
    give_up = time.time() + timeout
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale_after:
                    os.rmdir(path)
                    continue
            except OSError:
                pass
            if time.time() > give_up:
                raise RuntimeError(f"captcha queue lock stuck: {path}")
            time.sleep(0.02)
        except OSError as e:
            raise RuntimeError(f"cannot lock captcha queue {queue_dir}: {e}")
    try:
        yield
    finally:
        try:
            os.rmdir(path)
        except OSError:
            pass


def pop_token(queue_dir: str, wait: float = 0) -> str:
    """Take the oldest token from the farm queue (thread and process safe).

    The pop runs under :func:`_queue_lock`, so "list -> read -> rename to .used"
    is a single critical section: a token can be handed out exactly once, and a
    token that cannot be parsed is left in place instead of being burned.  The
    earlier designs (rename to a shared ``.used``; rename to a per-thread claim
    then read) both lost tokens on Windows -- see ``_queue_lock``.

    wait > 0 blocks up to that many seconds for the farm to produce a token.
    """
    deadline = time.time() + max(0.0, wait)
    max_age = float(os.environ.get("VMAX_TOKEN_MAX_AGE", "90"))
    last_err = ""
    while True:
        # stat while listing: a file can vanish between glob() and getmtime()
        aged = []
        for path in glob.glob(os.path.join(queue_dir, "tok_*.json")):
            try:
                aged.append((os.path.getmtime(path), path))
            except OSError:
                continue
        if not aged:
            if time.time() >= deadline:
                raise RuntimeError(f"captcha queue empty: {queue_dir} "
                                   f"(run vmax_tokens.py --n N --out-dir {queue_dir})")
            time.sleep(1.0)
            continue
        aged.sort()

        with _queue_lock(queue_dir):
            for _, src in aged:
                # Aliyun captcha tokens die fast (measured < ~2 min, PROTOCOL S6.6):
                # a stale entry is worthless and would waste a send-code slot.
                if max_age > 0:
                    try:
                        if time.time() - os.path.getmtime(src) > max_age:
                            os.replace(src, src + ".stale")
                            print(f"[captcha] dropped stale token {os.path.basename(src)} "
                                  f"(>{max_age:.0f}s old)", flush=True)
                            continue
                    except OSError:
                        continue
                try:
                    with open(src, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError) as e:
                    last_err = f"{type(e).__name__}: {e}"
                    _poplog({"src": src, "outcome": "read_error", "err": last_err})
                    continue
                param = data.get("param") if isinstance(data, dict) else str(data)
                if not param:
                    last_err = "empty param"
                    _poplog({"src": src, "outcome": "empty_param"})
                    continue
                try:
                    os.replace(src, src + ".used")
                except OSError as e:
                    last_err = f"claim failed: {type(e).__name__}: {e}"
                    _poplog({"src": src, "outcome": "claim_lost", "err": last_err})
                    continue
                _poplog({"src": src, "outcome": "ok", "param_len": len(param)})
                return param

        if time.time() >= deadline:
            raise RuntimeError(f"captcha queue had files but none readable: {queue_dir}"
                               + (f" ({last_err})" if last_err else ""))
        time.sleep(0.2)


def obtain_captcha(args, login_url: str) -> str:
    if args.captcha == "param":
        if not args.captcha_param:
            raise SystemExit("--captcha param requires --captcha-param")
        return args.captcha_param
    if args.captcha == "queue":
        return pop_token(args.queue_dir, getattr(args, "queue_wait", 0))
    from vmax_captcha import InpaintingSolver
    with InpaintingSolver(headless=not (args.headed or args.captcha == "manual"),
                          proxy=args.proxy, debug_dir=args.debug_dir) as sv:
        if args.captcha == "manual":
            return sv.solve_manual(login_url)
        if args.captcha == "assist":
            return sv.solve_assisted(login_url, args.assist_dir, use_stdin=args.stdin)
        return sv.solve(login_url, max_attempts=args.captcha_attempts)


def device_form_param(args, cli, login_url, phone: str, result: dict,
                       tag: str | None = None) -> str:
    """The site's own captcha-free submit path (R140, measured live).

    ``GET /login`` (sets the auth_device cookie) -> set auth_fingerprint ->
    ``GET /api/auth/signup-check``.  If the answer is ``requires_captcha: false``,
    ``POST /register`` accepts an empty ``captcha_verify_param`` -- measured on
    production: the form then answers 请先获取短信验证码 (i.e. the captcha gate
    passed) while the same body without the cookies answers Unsupported captcha
    platform.

    ``POST /register/send-code`` still needs a real token, so this only saves the
    *form* token.  Anything unexpected falls back to a paid token: one wasted
    token is cheaper than one wasted card.
    """
    fp = getattr(args, "device_id", None) or None
    who = tag or phone
    try:
        info = cli.prepare_device(fp, login_url=login_url)
    except Exception as e:  # noqa: BLE001 - any transport problem -> paid path
        result["device"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(f"[{who}] device path unavailable ({e}); falling back to a captcha token",
              flush=True)
        result["form_captcha"] = "device_fallback"
        return obtain_captcha(args, login_url)
    ok = info.get("requires_captcha") is False
    result["device"] = {"ok": ok, "signup_check": {k: v for k, v in info.items()
                                                   if not k.startswith("_")}}
    if ok:
        print(f"[{who}] device path accepted: /register needs no captcha token "
              f"(signup-check requires_captcha=false)", flush=True)
        result["form_captcha"] = "device"
        return ""
    print(f"[{who}] device path refused ({info}); solving a token for the form instead",
          flush=True)
    result["form_captcha"] = "device_fallback"
    return obtain_captcha(args, login_url)


PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
VIRTUAL_PREFIXES = ("170", "171")  # observed: rejected by the carrier check

# /register/send-code is documented as "向未注册手机号发送 register 用途验证码"
# (openapi: send_register_form_code_...), so a taken number is refused right there --
# before any /register write and before an SMS is spent.  The exact wording is not
# in the spec, so match conservatively and let "未注册" win.
PHONE_TAKEN_RULES = ("已注册", "已被注册", "已存在", "已被使用", "已绑定")


def is_phone_taken(msg) -> bool:
    """True when the server says this number already has an account."""
    text = (msg or "").strip()
    if not text or "未注册" in text or "未被注册" in text:
        return False
    return any(n in text for n in PHONE_TAKEN_RULES)


CODE_ERR_RULES = (
    ("rate_limited", "操作过于频繁"),
    ("code_locked", "次数过多"),
    ("code_expired", "过期"),
    ("code_wrong", "验证码不正确"),
    ("cooldown", "过于频繁"),
    ("captcha_rejected", "Captcha verification rejected"),
    ("captcha_rejected", "Unsupported captcha platform"),
    ("phone_format", "格式"),
)


def classify_register_error(msg: str | None) -> str:
    """Map a /register error string to a stable bucket.

    Observed policy (test env, 2026-10-26): a code tolerates 5 wrong attempts,
    the 6th returns "验证码错误次数过多，请重新获取"; re-sending after the 60s
    per-phone cooldown issues a fresh code and resets the counter.

    ``phone_taken`` is terminal: the number already has an account, so no amount
    of retrying will register it (and each retry costs a captcha token).
    """
    text = (msg or "").strip()
    if is_phone_taken(text):
        return "phone_taken"
    for kind, needle in CODE_ERR_RULES:
        if needle in text:
            return kind
    return "other" if text else "unknown"



def normalize_phone(raw: str) -> str:
    """Accept +86 / 86 / 0086 prefixes and separators, return 11 digits."""
    s = re.sub(r"[\s\-()]+", "", str(raw or ""))
    if s.startswith("+86"):
        s = s[3:]
    elif s.startswith("0086"):
        s = s[4:]
    elif s.startswith("86") and len(s) == 13:
        s = s[2:]
    return s


def register_one(args) -> dict:
    phone = normalize_phone(args.phone)
    # Validate before spending a captcha token: /register/send-code checks the
    # captcha first, so a bad number would burn a valid token.
    if not PHONE_RE.match(phone):
        raise VmaxError(f"phone format invalid: {phone!r} (need 11-digit mainland number)")
    if phone[:3] in VIRTUAL_PREFIXES and not getattr(args, "allow_virtual", False):
        raise VmaxError(f"phone {phone} is a virtual range ({phone[:3]}) rejected by the carrier check")
    username = args.username or gen_username()
    password = args.password or gen_password()
    result: dict = {"phone": phone, "username": username, "password": password,
                    "platform": args.platform, "ts": int(time.time())}

    cli = VmaxAuth(proxy=args.proxy, base=getattr(args, "base", None),
                   redirect_uri=getattr(args, "redirect", None))
    result["base"] = cli.base
    result["redirect_uri"] = cli.client_conf(args.platform)["redirect_uri"]
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    login_url = VmaxAuth.login_url(challenge, state=state, client=args.platform,
                                   base=cli.base, redirect_uri=cli.redirect_uri)

    # 1) captcha
    t0 = time.time()
    param = obtain_captcha(args, login_url)
    result["captcha_seconds"] = round(time.time() - t0, 1)
    print(f"[{phone}] captcha ok ({result['captcha_seconds']}s)", flush=True)

    # 2) send SMS
    # Do not spend a real SIM message while the write bucket is banned: send-code
    # is outside the bucket, so it would succeed and then /register would 429 until
    # the ban clears -- one wasted SMS plus one burnt captcha token.
    if getattr(cli, "pacer", None) is not None:
        _waited = cli.pacer.wait_if_banned(cli.pace_key)
        if _waited:
            result["waited_out_ban_s"] = round(_waited, 1)
    t_send = time.time()   # request start: an SMS may land before the response returns
    r = cli.send_register_code(phone, param, client=args.platform)
    result["send_code"] = r
    print(f"[{phone}] send-code {r}", flush=True)
    if not r.get("ok"):
        # /register/send-code only serves 未注册 numbers: a taken one is refused
        # right here, before any SMS is spent and before any /register write.
        # Classifying it is what turns that answer into exit 7 + "skip on resume"
        # in vmax_go / vmax_batch; a bare raise reported it as an ordinary
        # failure and retried the same (terminal) number on the next run.
        msg = str(r.get("message") or "")
        _reject(f"send-code rejected: {msg}", classify_register_error(msg), result,
                status=r.get("status"))
    last_send = t_send

    # 3+4) wait for the code, submit, and recover from a burnt code budget
    sms = build_sms(args.sms, args.sms_dir)
    resends = max(0, int(getattr(args, "sms_resends", 0) or 0))
    resend_no = 0
    while True:
        # since=last_send: after a resend, never accept the previous SMS
        code = sms.wait(phone, timeout=args.sms_timeout, since=last_send)
        print(f"[{phone}] sms code {code}", flush=True)

        def _submit(captcha_param: str) -> dict:
            o = cli.register_form(phone, code, username, password, verifier, challenge, state,
                                  captcha_param, invitation_code=args.invite,
                                  client=args.platform)
            return o

        policy = getattr(args, "form_captcha", "auto") or "auto"

        def next_param() -> str:
            """A token for the *form*: free while the device path is still trusted."""
            if policy == "device" and result.get("form_captcha") == "device":
                return ""
            return obtain_captcha(args, login_url)

        if policy == "device":
            param = device_form_param(args, cli, login_url, phone, result)
            out = _submit(param)
            if (result.get("form_captcha") == "device"
                    and classify_register_error(out.get("error") or "") == "captcha_rejected"):
                # /register ignored signup-check (or the trust expired between the
                # two calls).  Buy one token rather than lose the card.
                print(f"[{phone}] /register still asked for a captcha; solving one token",
                      flush=True)
                param = obtain_captcha(args, login_url)
                out = _submit(param)
                out["captcha_retry"] = True
                result["form_captcha"] = "device_retry"
        elif policy == "fresh":
            # /register may refuse a token already spent by send-code; solve a second one.
            param = obtain_captcha(args, login_url)
            out = _submit(param)
            result["form_captcha"] = "fresh"
        else:
            if policy == "reuse":
                # R154: the cert that sent the SMS was verified once already; a
                # second verify with the same param answers "Captcha verification
                # rejected".  Keep the policy (one card can re-test the hypothesis)
                # but never let it look like a normal run.
                print(f"[{phone}] policy=reuse: send-code already spent this token "
                      f"(R154) -- this submit is expected to be rejected; use it only "
                      f"to re-test that hypothesis", flush=True)
            out = _submit(param)
            # Reusing the send-code token buys nothing new for the form, but the
            # record must say so: an absent field reads as "unknown" in the
            # batch summary (R150).
            result["form_captcha"] = "reuse"
            if policy == "auto" and classify_register_error(out.get("error") or "") == "captcha_rejected":
                print(f"[{phone}] form captcha rejected (token likely consumed by send-code); "
                      f"re-solving one and retrying the form", flush=True)
                param = obtain_captcha(args, login_url)
                out = _submit(param)
                out["captcha_retry"] = True
                result["form_captcha"] = "auto_retry"
        # 503 「注册奖励服务暂不可用」 leaves the code unconsumed and creates nothing, so a
        # short backoff can save the card we already paid an SMS for.  The form token is
        # single-use, so every retry solves a fresh one.  The carrier-level 503
        # (「运营商验证暂不可用」) is number-specific: retrying that only burns tokens.
        max_503 = max(0, int(getattr(args, "retry_503", 1) or 0))
        tried_503 = 0
        while True:
            result["register"] = {k: v for k, v in out.items() if k != "access_token"}
            if out.get("code"):
                break
            err = out.get("error") or ""
            kind = classify_register_error(err)
            result["register_error_kind"] = kind
            if not (out.get("status") == 503 and tried_503 < max_503
                    and "运营商" not in err):
                break
            tried_503 += 1
            wait_s = float(getattr(args, "wait_503", 10) or 0)
            print(f"[{phone}] 503 {err.strip()[:40]!r}: retry {tried_503}/{max_503} "
                  f"in {wait_s:.0f}s (no SMS, fresh captcha)", flush=True)
            if wait_s > 0:
                time.sleep(wait_s)
            param = next_param()
            out = _submit(param)
            result.setdefault("retries_503", []).append(
                {"wait_s": wait_s, "status": out.get("status"),
                 "error": (out.get("error") or "")[:120]})
        if out.get("code"):
            break
        if kind in ("code_locked", "code_expired", "code_wrong") and resend_no < resends:
            resend_no += 1
            wait_s = float(os.environ.get("VMAX_RESEND_WAIT", "62"))
            cooldown = wait_s - (time.time() - last_send)
            if cooldown > 0:
                print(f"[{phone}] {kind}: waiting {cooldown:.0f}s for the resend cooldown", flush=True)
                time.sleep(cooldown)
            param = obtain_captcha(args, login_url)
            t_send = time.time()
            r = cli.send_register_code(phone, param, client=args.platform)
            result.setdefault("resends", []).append(r)
            print(f"[{phone}] resend#{resend_no} {r}", flush=True)
            if not r.get("ok"):
                msg = str(r.get("message") or "")
                _reject(f"resend rejected: {msg}", classify_register_error(msg), result,
                        status=r.get("status"))
            last_send = t_send
            continue
        # The 429 ban is signalled by the status code only (the body is a generic
        # "操作过于频繁" page), and the runners abort on
        # `kind == rate_limited or "429" in error` -- so keep both.
        status = out.get("status")
        detail = err or str(status)
        _reject(f"register failed: {status} {detail}", kind, result, status=status,
                payload=out)
    print(f"[{phone}] register ok, code={out['code'][:12]}...", flush=True)
    # From this instant the account exists while the row exists only in memory.
    # If the caller dies before writing results.jsonl, the number comes back as an
    # ordinary `phone_taken` on resume and the generated password is gone for good
    # (the reset flow costs another SMS).  The journal keeps that window closed.
    journal.append(getattr(args, "journal", None), phone, "registered",
                   username=username, password=password)

    # 5) exchange the code
    tok = cli.exchange_code(out["code"], verifier, client=args.platform)
    result["token"] = tok
    result["uid"] = (tok.get("user") or {}).get("uid") if isinstance(tok.get("user"), dict) else None
    print(f"[{phone}] token ok, expires_in={tok.get('expires_in')}", flush=True)
    result["status"] = "ok"
    # Second line: with the uid the row can be rebuilt later with zero network calls.
    journal.append(getattr(args, "journal", None), phone, "ok",
                   username=username, password=password, uid=result.get("uid"))
    return result


def main():
    ap = argparse.ArgumentParser(description="vobilemax single account registration")
    ap.add_argument("--phone", required=True)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=list(CLIENTS))
    ap.add_argument("--invite", default="")
    ap.add_argument("--captcha", default="auto",
                    choices=["auto", "manual", "assist", "param", "queue"])
    ap.add_argument("--captcha-param", default="")
    ap.add_argument("--queue-dir", default="tokens",
                    help="token queue directory for --captcha queue")
    ap.add_argument("--queue-wait", type=float, default=0,
                    help="seconds to wait for a token when the queue is empty")
    ap.add_argument("--allow-virtual", action="store_true",
                    help="do not pre-reject 170/171 numbers")
    ap.add_argument("--captcha-attempts", type=int, default=6)
    ap.add_argument("--assist-dir", default="assist")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--sms", default="prompt")
    ap.add_argument("--sms-dir", default="sms_inbox")
    ap.add_argument("--sms-timeout", type=int, default=180)
    ap.add_argument("--retry-503", type=int, default=1, metavar="N",
                    help="POST /register 503 (reward service down) -> wait and retry N times "
                         "with a fresh captcha token; the SMS code is NOT consumed, so this "
                         "saves the card (default 1, 0 = never). The carrier-level 503 "
                         "(运营商验证暂不可用) is never retried: it is number-specific")
    ap.add_argument("--wait-503", type=float, default=10.0, metavar="S",
                    help="seconds to sleep before a 503 retry (default 10)")
    ap.add_argument("--form-captcha", default="auto",
                    choices=["auto", "reuse", "fresh", "device"],
                    help="captcha token policy for POST /register: auto=reuse then retry with a "
                         "fresh token on rejection, reuse=never re-solve, fresh=always solve anew")
    ap.add_argument("--sms-resends", type=int, default=0,
                    help="on 验证码不正确/次数过多, re-send after the 60s cooldown (max N)")
    ap.add_argument("--journal", default=None,
                    help="append-only write-ahead log: /register 303 puts the credentials "
                         "on disk before the caller can die, so a resumed run can rebuild "
                         "the row with 0 token / 0 SMS (default off here; the batch runners "
                         "use <out>.journal)")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--base", default=None,
                    help="auth base URL, e.g. https://test-auth.vobilemax.cn (env VMAX_BASE)")
    ap.add_argument("--redirect", default=None,
                    help="OAuth redirect_uri override (test env: https://test-dreammaker.vobilemax.cn/auth/callback)")
    ap.add_argument("--debug-dir", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    try:
        result = register_one(args)
    except (VmaxError, SmsTimeout, RuntimeError) as e:
        result = {"phone": args.phone, "status": "error", "error": str(e), "ts": int(time.time())}
        print(f"[{args.phone}] FAILED: {e}", flush=True)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
