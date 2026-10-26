# -*- coding: utf-8 -*-
"""d1jiema.com (第一接码) API client.

API: https://api.d1jiema.com/zc/data.php
  code=leftAmount | getPhone | getMsg | release | block | queryUsed

Token comes from env D1JIEMA_TOKEN or the CLI flag.
"""
from __future__ import annotations

import os
import random
import re
import time
import urllib.parse
import urllib.request

BASE = "https://api.d1jiema.com/zc/data.php"
TOKEN = os.environ.get("D1JIEMA_TOKEN", "")
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36")


class SmsError(RuntimeError):
    pass


def _call(code: str, token: str | None = None, timeout: int = 30, **params) -> str:
    q = {"code": code, "token": token or TOKEN}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    url = BASE + "?" + urllib.parse.urlencode(q)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace").strip()
    except Exception as exc:  # network layer
        raise SmsError(f"request failed: {exc!r}") from exc


def balance(token: str | None = None) -> float:
    txt = _call("leftAmount", token)
    try:
        return float(txt)
    except ValueError:
        raise SmsError(f"balance error: {txt[:200]}")


def get_phone(keyword: str = "飞书", province: str | None = None,
              card_type: str = "实卡", phone: str | None = None,
              token: str | None = None) -> str:
    """Ask the provider for a number. Returns the phone number string."""
    for attempt in range(3):
        txt = _call("getPhone", token, keyWord=keyword, province=province,
                    cardType=card_type, phone=phone)
        if txt.startswith("ERROR"):
            last = txt
            time.sleep(2 + attempt * 2)
            continue
        m = re.search(r"1[3-9]\d{9}", txt)
        if m:
            return m.group(0)
        last = txt
        time.sleep(2)
    raise SmsError(f"getPhone failed: {last[:200]}")


def get_msg(phone: str, keyword: str = "飞书", token: str | None = None) -> str:
    return _call("getMsg", token, phone=phone, keyWord=keyword)


CODE_PATTERNS = (
    r"(?:验证码|校验码|动态码|code)[^0-9]{0,12}(\d{4,8})",
    r"(\d{4,8})[^0-9]{0,6}(?:是|为)?[^0-9]{0,6}(?:验证码|校验码)",
    r"(?<!\d)(\d{6})(?!\d)",
)


def extract_code(text: str) -> str | None:
    for pat in CODE_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    return None


def poll_code(phone: str, keywords=("飞书", "Lark", "验证码"),
              timeout: int = 180, interval: int = 8,
              token: str | None = None, logger=print) -> str:
    """Poll until an SMS with a code arrives. Raises SmsError on timeout."""
    t0 = time.time()
    seen_pending = 0
    while time.time() - t0 < timeout:
        for kw in keywords:
            try:
                msg = get_msg(phone, kw, token)
            except SmsError as exc:
                logger(f"    [sms] kw={kw} err={exc}")
                continue
            if not msg or msg.startswith("ERROR"):
                continue
            if "尚未收到" in msg or "not received" in msg.lower():
                seen_pending += 1
                continue
            code = extract_code(msg)
            if code:
                logger(f"    [sms] +{int(time.time()-t0)}s kw={kw} -> {msg[:120]}")
                return code
            logger(f"    [sms] kw={kw} text-without-code: {msg[:100]}")
        time.sleep(interval + random.uniform(0, 2))
    raise SmsError(f"no sms code for {phone} within {timeout}s (pending polls={seen_pending})")


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=TOKEN)
    ap.add_argument("--balance", action="store_true")
    ap.add_argument("--phone", action="store_true", help="grab one number")
    ap.add_argument("--keyword", default="飞书")
    ap.add_argument("--card-type", default="实卡", choices=["实卡", "虚卡", "全部"])
    ap.add_argument("--msg", metavar="PHONE")
    ap.add_argument("--poll", metavar="PHONE")
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args()
    if a.balance:
        print(json.dumps({"balance": balance(a.token)}, ensure_ascii=False))
    if a.phone:
        print(json.dumps({"phone": get_phone(a.keyword, card_type=a.card_type, token=a.token)},
                         ensure_ascii=False))
    if a.msg:
        print(get_msg(a.msg, a.keyword, a.token))
    if a.poll:
        print(json.dumps({"code": poll_code(a.poll, timeout=a.timeout, token=a.token)},
                         ensure_ascii=False))