# -*- coding: utf-8 -*-
"""d1jiema (d1jiema.com) upstream client.

Endpoint: GET https://api.d1jiema.com/zc/data.php
The upstream rejects requests without a browser User-Agent / Referer (HTTP 500),
so every call sends both. All failures come back as plain text starting with
"ERROR:", an empty mailbox as a body containing "[尚未收到]".
"""
from __future__ import annotations

import random
import re
import time
import uuid
from typing import Optional

import requests

from . import db

BASE_DEFAULT = "https://api.d1jiema.com/zc/data.php"
REFERER = "https://www.d1jiema.com/api.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

S = requests.Session()
S.trust_env = False

PACE_LOCK = "d1jiema_pace_ms"

# Upstream success bodies look like "1935823746|0/0.26/【豆包】验证码 932517，..." i.e.
# "messageId|price/balance/content". Both the message id and the price/balance are
# ours, never the buyer's, so they must be stripped before anything leaves the box.
NO_MSG = "0|0"
_PRICE_PREFIX = re.compile(
    r"^\s*(?:[0-9]{4,}\s*\|\s*)?[0-9]+(?:\.[0-9]+)?\s*/\s*[0-9]+(?:\.[0-9]+)?\s*/\s*")
_EMPTY_HINTS = ("尚未收到", "请5秒后再收取", "请5秒后")


def clean(raw: str) -> str:
    """Normalize a d1jiema body: drop message id + price/balance, keep the SMS text.

    Idempotent, so callers may clean the same string twice. Empty mailboxes become
    the neutral "0|0" placeholder so the response keeps the common upstream shape
    without leaking the vendor's wording.
    """
    t = (raw or "").strip()
    if not t:
        return NO_MSG
    if any(h in t for h in _EMPTY_HINTS):
        return NO_MSG
    if t[:6].upper().startswith("ERROR"):
        return t
    m = _PRICE_PREFIX.match(t)
    if m:
        body = t[m.end():].strip()
        return ("1|" + body) if body else NO_MSG
    return t



class D1Error(Exception):
    pass


class D1Busy(D1Error):
    def __init__(self, message: str, retry_after: int = 2):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def token() -> str:
    return (db.get_setting("d1jiema_token", "") or "").strip()


def base() -> str:
    raw = (db.get_setting("d1jiema_base", BASE_DEFAULT) or BASE_DEFAULT).strip()
    if not raw:
        raw = BASE_DEFAULT
    if "data.php" not in raw:
        raw = raw.rstrip("/") + "/zc/data.php"
    return raw


def keyword() -> str:
    return (db.get_setting("d1jiema_keyword", "豆包") or "豆包").strip()


def sms_prefix() -> str:
    return (db.get_setting("d1jiema_sms_prefix", "[豆包]") or "[豆包]").strip()


def enabled() -> bool:
    return (db.get_setting("d1jiema_enabled", "1") or "1") == "1" and bool(token())


def _gap_ms() -> int:
    try:
        return max(120, min(5000, int(db.get_setting("d1jiema_gap_ms", "300") or 300)))
    except Exception:
        return 300


# ---------------------------------------------------------------- cross-worker helpers
def _lease(name: str, ttl: int = 45) -> str:
    """Claim a DB lease shared by every uvicorn worker."""
    tok = uuid.uuid4().hex[:12]
    now = int(time.time())
    try:
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO locks(name,owner,until) VALUES(?,'',0)", (name,))
            cur = c.execute("UPDATE locks SET owner=?, until=? WHERE name=? AND until<?",
                            (tok, now + ttl, name, now))
            return tok if cur.rowcount == 1 else ""
    except Exception:
        return ""


def _unlease(name: str, tok: str) -> None:
    if not tok:
        return
    try:
        db.conn().execute("UPDATE locks SET until=0 WHERE name=? AND owner=?", (name, tok))
    except Exception:
        pass


def pace(gap_ms: Optional[int] = None) -> bool:
    """Global request pacing (millisecond precision) so a burst cannot hammer upstream."""
    gap = gap_ms if gap_ms is not None else _gap_ms()
    now = int(time.time() * 1000)
    try:
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO locks(name,owner,until) VALUES(?,'',0)", (PACE_LOCK,))
            row = c.execute("SELECT until FROM locks WHERE name=?", (PACE_LOCK,)).fetchone()
            if row and int(row["until"] or 0) > now:
                return False
            c.execute("UPDATE locks SET until=? WHERE name=?", (now + gap, PACE_LOCK,))
        return True
    except Exception:
        return True


def wait_turn(max_wait: float = 3.0) -> bool:
    """Block until this worker owns the next pacing slot (used by the purchase path)."""
    deadline = time.time() + max_wait
    while True:
        if pace():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05 + random.random() * 0.08)


def buy_lease() -> str:
    return _lease("d1jiema_buy", 40)


def buy_unlease(tok: str) -> None:
    _unlease("d1jiema_buy", tok)


def card_lease(card_id: int) -> str:
    return _lease("d1buy:%d" % int(card_id), 60)


def card_unlease(card_id: int, tok: str) -> None:
    _unlease("d1buy:%d" % int(card_id), tok)


def poll_lease(phone: str) -> str:
    return _lease("d1poll:%s" % str(phone)[-11:], 8)


def poll_unlease(phone: str, tok: str) -> None:
    _unlease("d1poll:%s" % str(phone)[-11:], tok)


# ---------------------------------------------------------------- HTTP
def _request(params: dict, timeout: int = 25, retries: int = 0) -> str:
    tok = token()
    if not tok:
        raise D1Error("d1jiema Token 未配置")
    q = dict(params or {})
    q["token"] = tok
    # Only UA + Referer: the upstream WAF 500s when extra negotiation headers are sent.
    headers = {"User-Agent": UA, "Referer": REFERER}
    text = ""
    status = 0
    last_exc = None
    for attempt in range(max(0, retries) + 1):
        try:
            r = S.get(base(), params=q, headers=headers, timeout=timeout)
            status = r.status_code
            text = (r.text or "").strip()
        except requests.Timeout:
            last_exc = D1Busy("d1jiema 请求超时", retry_after=3)
        except Exception as e:
            last_exc = D1Busy("d1jiema 连接失败: %s" % type(e).__name__, retry_after=5)
        if status == 500 and attempt < retries:
            time.sleep(0.6 + random.random() * 0.7)
            continue
        break
    if last_exc is not None and not text:
        raise last_exc
    if status != 200:
        raise D1Busy("d1jiema 上游 HTTP %d" % status, retry_after=5)
    if not text:
        raise D1Busy("d1jiema 返回空响应", retry_after=3)
    low = text[:40].upper()
    if low.startswith("ERROR"):
        msg = text.split(":", 1)[1].strip() if ":" in text else text
        msg = msg or "上游拒绝"
        if any(k in msg for k in ("余额", "不足", "禁用", "封禁", "频率")):
            raise D1Busy("d1jiema: " + msg[:120], retry_after=20)
        raise D1Error("d1jiema: " + msg[:160])
    return text


def balance() -> str:
    return _request({"code": "leftAmount"}, timeout=20, retries=1)


def _norm_phone(text: str) -> str:
    s = re.sub(r"[\s\-()]", "", text or "")
    if s.startswith("+86"):
        s = s[3:]
    elif s.startswith("0086"):
        s = s[4:]
    elif s.startswith("86") and len(s) == 13:
        s = s[2:]
    if not re.fullmatch(r"1\d{10}", s):
        raise D1Error("d1jiema 返回异常号码: %s" % (text or "")[:40])
    return s


def get_phone(kw: str = "", card_type: str = "全部", phone: str = "", timeout: int = 30) -> str:
    """Buy/allocate one number. Raises D1Busy when upstream is throttling."""
    if not enabled():
        raise D1Error("d1jiema 通道未启用")
    if not wait_turn():
        raise D1Busy("d1jiema 通道繁忙，稍后重试", retry_after=2)
    params = {"code": "getPhone", "keyWord": (kw or keyword())}
    ct = (card_type or "").strip()
    if ct in ("实卡", "虚卡", "全部"):
        params["cardType"] = ct
    if phone:
        params["phone"] = phone
    return _norm_phone(_request(params, timeout=timeout))


def get_msg(phone: str, kw: str = "", timeout: int = 25) -> str:
    """SMS body with our cost/id fields removed ("0|0" while nothing arrived)."""
    if not phone:
        raise D1Error("缺少手机号")
    raw = _request({"code": "getMsg", "phone": phone, "keyWord": (kw or keyword())},
                   timeout=timeout, retries=1)
    return clean(raw)


def has_msg(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and "尚未收到" not in t and t != NO_MSG


def test() -> dict:
    if not token():
        return {"ok": False, "message": "d1jiema Token 未配置"}
    try:
        bal = balance()
    except D1Busy as e:
        return {"ok": False, "message": str(e)}
    except D1Error as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": "d1jiema 测试失败: %s" % type(e).__name__}
    try:
        bal_f = float(bal)
    except Exception:
        bal_f = -1
    msg = "d1jiema 连接正常，余额 %s 元；关键词=%s；卡类型=%s" % (
        bal, keyword(), "实卡/虚卡（按号段选择）")
    if bal_f >= 0 and bal_f < 1:
        msg += "；余额偏低，请及时充值"
        return {"ok": False, "message": msg}
    return {"ok": True, "message": msg}