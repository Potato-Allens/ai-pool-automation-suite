# -*- coding: utf-8 -*-
"""Number providers: local pool, SMS-Activate, 5SIM, NexSMS premium."""
from __future__ import annotations
import json, os, random, re, threading, time, uuid
from typing import Optional

import requests

from . import d1jiema, db

S = requests.Session()
S.trust_env = False

COUNTRIES = {
    "us": {"name": "美国", "flag": "🇺🇸", "sa": 12, "fs": "usa", "dial": "+1"},
    "gb": {"name": "英国", "flag": "🇬🇧", "sa": 16, "fs": "england", "dial": "+44"},
    "hk": {"name": "香港", "flag": "🇭🇰", "sa": 14, "fs": "hongkong", "dial": "+852"},
    "cn": {"name": "中国", "flag": "🇨🇳", "sa": 0, "fs": "china", "dial": "+86"},
}

# What a card holder can pick. A card may pick exactly one of these, once.
# "us"/"gb"/"hk" follow the admin's global provider setting (NexSMS in production);
# "cn_real"/"cn_virtual" always go through d1jiema with the matching card type.
CHOICES = {
    "us": {"name": "美国", "flag": "🇺🇸", "provider": "", "country": "us",
           "card_type": "", "sub": "United States"},
    "gb": {"name": "英国", "flag": "🇬🇧", "provider": "", "country": "gb",
           "card_type": "", "sub": "United Kingdom"},
    "hk": {"name": "香港", "flag": "🇭🇰", "provider": "", "country": "hk",
           "card_type": "", "sub": "Hong Kong"},
    "cn_real": {"name": "国内实卡", "flag": "🇨🇳", "provider": "d1jiema", "country": "cn",
                "card_type": "实卡", "sub": "China · Real SIM"},
    "cn_virtual": {"name": "国内虚卡", "flag": "🇨🇳", "provider": "d1jiema", "country": "cn",
                   "card_type": "虚卡", "sub": "China · Virtual"},
}

SA_BASE = "https://api.sms-activate.org/stubs/handler_api.php"
FS_BASE = "https://5sim.net/v1"
NEX_DEFAULT_BASE = "https://api.nexsms.net"

_NEX_CACHE = {}
_NEX_LOCK = threading.Lock()
_NEX_BUY_LOCK = threading.Lock()
_NEX_WAIT_LOCK = threading.Lock()
_NEX_WAITERS = 0
_NEX_POLL_CACHE = {}
_NEX_POLL_LOCK = threading.Lock()
_NEX_POLL_LOCKS = {}
_NEX_POLL_LOCKS_LOCK = threading.Lock()
_NEX_HTTP_LOCK = threading.Lock()
_NEX_LAST_HTTP = [0.0]
_NEX_REFILLING = set()
_NEX_REFILL_LOCK = threading.Lock()

# Direct allocation (queue removed): one buyer per country at a time, everyone else
# waits a couple of seconds for fresh stock instead of holding a queue ticket.
_ALLOC_GUARD = threading.Lock()
_ALLOC_LOCKS = {}
_ALLOC_WAITERS = {}
_SUPPLY_BLOCK = [0.0, ""]


class ProviderError(Exception):
    pass


class ProviderBusy(ProviderError):
    """Upstream is throttling or the local pool is drained; retry shortly."""

    def __init__(self, message: str, retry_after: int = 8):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def _sa_key() -> str:
    return (db.get_setting("sms_activate_key", "") or "").strip()


def _fs_key() -> str:
    return (db.get_setting("fivesim_key", "") or "").strip()


def _nex_key() -> str:
    return (db.get_setting("nexsms_key", "") or "").strip()


def _nex_base() -> str:
    return (db.get_setting("nexsms_base", NEX_DEFAULT_BASE) or NEX_DEFAULT_BASE).strip().rstrip("/")


_PROXY_SID = {"v": "", "ts": 0.0}
_PROXY_LOCK = threading.Lock()


def _nex_proxy_rotate() -> None:
    with _PROXY_LOCK:
        _PROXY_SID["v"] = ""
        _PROXY_SID["ts"] = 0.0


def _nex_proxy() -> str:
    """Optional outbound proxy, e.g. http://user:pass@host:3000.

    Supports `{sid}` in the user part: it is replaced by a rotating random session id,
    which gives a fresh exit IP and dodges per-IP rate limits on the proxy itself.
    """
    raw = (db.get_setting("nexsms_proxy", "") or "").strip()
    if not raw or ("{sid}" not in raw and "{rand}" not in raw):
        return raw
    now = time.time()
    with _PROXY_LOCK:
        if not _PROXY_SID["v"] or now - _PROXY_SID["ts"] > 600:
            _PROXY_SID["v"] = uuid.uuid4().hex[:8]
            _PROXY_SID["ts"] = now
        sid = _PROXY_SID["v"]
    return raw.replace("{sid}", sid).replace("{rand}", sid)


def _nex_project() -> str:
    return (db.get_setting("nexsms_project", "豆包") or "豆包").strip()


def _nex_sms_prefix() -> str:
    return (db.get_setting("nexsms_sms_prefix", "[豆包]") or "[豆包]").strip()


def _nex_type() -> int:
    try:
        return max(1, min(3, int(db.get_setting("nexsms_type", "1") or 1)))
    except Exception:
        return 1


def _nex_expiry() -> int:
    try:
        return max(0, min(6, int(db.get_setting("nexsms_expiry", "0") or 0)))
    except Exception:
        return 0


def cooldown_left() -> int:
    """Seconds left before we are allowed to hit the upstream again."""
    try:
        return max(0, int(db.get_setting("nexsms_cooldown_until", "0") or 0) - int(time.time()))
    except Exception:
        return 0


def _cooldown_base() -> int:
    try:
        return max(5, min(300, int(db.get_setting("nexsms_cooldown_sec", "30") or 30)))
    except Exception:
        return 30


def cooldown_set(seconds: int = 0) -> int:
    """Back off after throttling; repeated 429s double the wait, capped at 5 minutes."""
    try:
        streak = int(db.get_setting("nexsms_429_streak", "0") or 0) + 1
    except Exception:
        streak = 1
    base = seconds or _cooldown_base()
    wait = int(min(3600, base * (2 ** min(5, streak - 1))))
    db.set_setting("nexsms_429_streak", str(streak))
    db.set_setting("nexsms_cooldown_until", str(int(time.time()) + wait))
    return wait


def cooldown_clear() -> None:
    db.set_setting("nexsms_cooldown_until", "0")
    db.set_setting("nexsms_429_streak", "0")


def _nex_http(method: str, path: str, **kwargs):
    key = _nex_key()
    if not key:
        raise ProviderError("NexSMS API Key 未配置")
    params = dict(kwargs.pop("params", {}) or {})
    params["apiKey"] = key
    headers = {"User-Agent": "SMSGate", "Accept": "application/json"}
    headers.update(kwargs.pop("headers", {}) or {})
    # Light pacing: upstream counts requests per minute, bursts trigger 429.
    px = _nex_proxy()
    proxies = {"http": px, "https": px} if px else None
    with _NEX_HTTP_LOCK:
        gap = 0.15 - (time.time() - _NEX_LAST_HTTP[0])
        if gap > 0:
            time.sleep(gap)
        try:
            r = S.request(method, _nex_base() + path, params=params, headers=headers, timeout=30,
                          proxies=proxies, **kwargs)
        except Exception as e:
            if proxies and ("Proxy" in type(e).__name__ or "proxy" in str(e).lower()):
                # Proxy down: fall back to a direct request instead of failing the user.
                try:
                    r = S.request(method, _nex_base() + path, params=params, headers=headers,
                                  timeout=30, **kwargs)
                except Exception as e2:
                    raise ProviderError("NexSMS 请求失败: %s" % type(e2).__name__)
            else:
                raise ProviderError("NexSMS 请求失败: %s" % type(e).__name__)
        _NEX_LAST_HTTP[0] = time.time()
    if r.status_code == 429:
        if px:
            # That proxy exit IP is throttled: rotate the session and retry shortly.
            _nex_proxy_rotate()
            raise ProviderBusy("上游线路被限速，已切换线路", retry_after=6)
        # Direct: Cloudflare sends the real ban window (error 1015); honour it exactly.
        try:
            ra = int(r.headers.get("retry-after") or 0)
        except Exception:
            ra = 0
        wait = cooldown_set(min(3600, ra) if ra > 0 else 0)
        raise ProviderBusy("上游通道限流，约 %d 分钟后自动恢复" % max(1, wait // 60),
                           retry_after=wait)
    try:
        d = r.json()
    except Exception:
        raise ProviderError("NexSMS 返回格式异常（HTTP %d）" % r.status_code)
    try:
        code = int(d.get("code") if d.get("code") is not None else 0)
    except Exception:
        code = -1
    if r.status_code != 200 or code != 0:
        msg = d.get("message") or d.get("errorCode") or ("HTTP %d" % r.status_code)
        if int(code) == 429:
            if px:
                _nex_proxy_rotate()
            raise ProviderBusy("上游接口繁忙，已切换线路", retry_after=5)
        raise ProviderError("NexSMS: %s" % str(msg)[:100])
    return d.get("data")


def _nex_region_match(cate_name: str, country: str) -> bool:
    s = (cate_name or "").upper()
    words = {
        "us": ("美国", "US", "USA", "UNITED STATES"),
        "gb": ("英国", "UK", "GB", "UNITED KINGDOM"),
        "hk": ("香港", "HK", "HONG KONG"),
    }.get(country, ())
    return any(w in s for w in words)


def _nex_cache_key(country: str, project: str, ptype: int) -> str:
    import hashlib
    h = hashlib.md5(("%s|%s" % (_nex_base(), project)).encode("utf-8")).hexdigest()[:8]
    return "nexsms_cache_%s_%d_%s" % (country, ptype, h)


def _nex_cache_load(country: str, project: str, ptype: int) -> list:
    """Project scan results survive restarts, so a fresh worker never re-hammers upstream."""
    try:
        raw = db.get_setting(_nex_cache_key(country, project, ptype), "")
        if not raw:
            return []
        d = json.loads(raw)
        if not isinstance(d, dict):
            return []
        if time.time() - float(d.get("ts") or 0) > 3600:
            return []
        return [x for x in (d.get("list") or []) if isinstance(x, dict)]
    except Exception:
        return []


def _nex_cache_save(country: str, project: str, ptype: int, found: list) -> None:
    try:
        db.set_setting(_nex_cache_key(country, project, ptype),
                       json.dumps({"ts": int(time.time()), "list": found}, ensure_ascii=False))
    except Exception:
        pass


def _nex_candidates(country: str) -> list:
    """All matching doubao items, best first (region -> stock -> type -> success -> quantity)."""
    manual = (db.get_setting("nexsms_app_id", "") or "").strip()
    preferred_type = _nex_type()
    if manual:
        try:
            return [{"appId": int(manual), "type": preferred_type, "name": "手动 appId",
                     "cate_id": 0, "cate_name": "", "quantity": 1, "price": 0.0,
                     "success": 0.0, "region_ok": True, "manual": True}]
        except Exception:
            raise ProviderError("NexSMS appId 配置不正确")

    project = _nex_project()
    key = _nex_key()
    if not key:
        raise ProviderError("NexSMS API Key 未配置")
    cache_key = (_nex_base(), key, project, country, preferred_type)
    now = time.time()
    with _NEX_LOCK:
        hit = _NEX_CACHE.get(cache_key)
        if hit and now - hit[0] < 300:
            return [dict(x) for x in hit[1]]
    disk = _nex_cache_load(country, project, preferred_type)
    if disk:
        with _NEX_LOCK:
            _NEX_CACHE[cache_key] = (now, [dict(x) for x in disk])
        return [dict(x) for x in disk]

    cats = _nex_http("GET", "/api/premium/categories") or {}
    cat_list = cats.get("list") or []
    type_ids = []
    for t in cats.get("types") or []:
        try:
            type_ids.append(int(t.get("id")))
        except Exception:
            pass
    order = [preferred_type] + [t for t in type_ids if t != preferred_type] + [1, 2, 3]
    order = list(dict.fromkeys(order))

    # Region-matching categories first (usually 1-2), then the rest as fallback.
    region_cats = [c for c in cat_list if _nex_region_match(str(c.get("name") or ""), country)]
    passes = []
    if region_cats:
        passes.append(region_cats)
    rest = [c for c in cat_list if c not in region_cats]
    if rest:
        passes.append(rest)
    if not passes:
        passes = [[]]
    found = []
    for ptype in order:
        for group in passes:
            for cat in group:
                try:
                    cate_id = int(cat.get("id"))
                except Exception:
                    continue
                cate_name = str(cat.get("name") or "")
                page = 1
                while page <= 12:
                    data = _nex_http("GET", "/api/premium/list", params={
                        "cateId": cate_id, "type": ptype, "page": page, "pageSize": 100,
                        "sortBy": "quantity", "sortOrder": "desc",
                    }) or {}
                    items = data.get("list") or []
                    for item in items:
                        name = str(item.get("name") or "")
                        if project not in name:
                            continue
                        try:
                            qty = int(item.get("quantity") or 0)
                        except Exception:
                            qty = 0
                        try:
                            price = float(item.get("price") or 0)
                        except Exception:
                            price = 0.0
                        try:
                            success = float((item.get("successInfo") or {}).get("successRate") or 0)
                        except Exception:
                            success = 0.0
                        found.append({
                            "appId": int(item.get("id")), "type": int(item.get("type") or ptype),
                            "name": name, "cate_id": cate_id, "cate_name": cate_name,
                            "quantity": qty, "price": price, "success": success,
                            "region_ok": _nex_region_match(cate_name, country),
                        })
                    # Enough: a stocked project in this category beats paging to the end.
                    if any(x["quantity"] > 0 for x in found):
                        break
                    total = int(data.get("total") or 0)
                    if not items or page * 100 >= total:
                        break
                    page += 1
            # Stop after the first pass that matched; fallback passes cost upstream calls.
            if found:
                break
        # Keep looking at the next card type when this one only has sold-out items:
        # e.g. 豆包-港卡 is type 2 (重启卡) while type 1 has zero stock.
        if any(x["quantity"] > 0 for x in found):
            break

    if not found:
        raise ProviderError("NexSMS 优选未找到『%s』项目，请检查 API Key 或项目关键词" % project)
    found.sort(key=lambda x: (x["quantity"] > 0, x["region_ok"], x["type"] == preferred_type,
                              x.get("success") or 0, x["quantity"]), reverse=True)
    with _NEX_LOCK:
        _NEX_CACHE[cache_key] = (now, [dict(x) for x in found])
    _nex_cache_save(country, project, preferred_type, found)
    return found


def _nex_find_app(country: str) -> dict:
    cands = _nex_candidates(country)
    if not cands:
        raise ProviderError("NexSMS 优选未找到『%s』项目" % _nex_project())
    return dict(cands[0])


def _nex_phone_country(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("1") and len(d) == 11:
        return "us"
    if d.startswith("44"):
        return "gb"
    if d.startswith("852"):
        return "hk"
    return ""


def _nex_prefix_ok(content: str, prefix: str) -> bool:
    """Only accept SMS whose leading tag matches, e.g. "[豆包]" also matches "【豆包】"."""
    text = (content or "").lstrip(chr(0xFEFF) + " " + chr(9) + chr(13) + chr(10))
    p = (prefix or "").strip()
    if not p:
        return True
    openers = (chr(91), chr(12304))
    closers = (chr(93), chr(12305))
    if len(p) >= 2 and p[0] in openers and p[-1] in closers:
        core = p[1:-1].strip()
        if not core:
            return True
        if not text or text[0] not in openers:
            return False
        end = text.find(closers[0] if text[0] == openers[0] else closers[1], 1)
        if end < 0:
            return False
        return text[1:end].strip() == core
    return text.startswith(p)


def enabled_countries() -> list[str]:
    raw = db.get_setting("allow_countries", "us,gb") or "us,gb"
    return [c.strip() for c in raw.split(",") if c.strip() in COUNTRIES and c.strip() != "cn"]


def enabled_choices() -> list[str]:
    """Choices shown on the public page, in a stable order."""
    raw = (db.get_setting("allow_choices", "") or "").strip()
    if not raw:
        base = list(enabled_countries())
        base += ["cn_real", "cn_virtual"]
    else:
        # allow_choices is the master switch; the legacy allow_countries list still
        # gates the international entries so both admin controls stay meaningful.
        allowed_countries = enabled_countries()
        base = [c.strip() for c in raw.split(",") if c.strip() in CHOICES]
        base = [c for c in base if c.startswith("cn_") or c in allowed_countries]
    if not d1jiema.enabled():
        base = [c for c in base if c not in ("cn_real", "cn_virtual")]
    order = ["us", "gb", "hk", "cn_real", "cn_virtual"]
    return [c for c in order if c in base]


def choice_info(choice: str) -> dict:
    return dict(CHOICES.get((choice or "").strip().lower()) or {})


def provider_for_choice(choice: str) -> str:
    info = CHOICES.get(choice) or {}
    if info.get("provider"):
        return str(info["provider"])
    return db.get_setting("provider", "pool") or "pool"


def number_choice(number) -> str:
    """Which choice a number belongs to (legacy rows fall back to country)."""
    try:
        val = number["choice"] if "choice" in number.keys() else ""
    except Exception:
        val = ""
    if val:
        return str(val)
    prov = (number["provider"] if "provider" in number.keys() else "") or ""
    if prov == "d1jiema":
        return "cn_real"
    return str(number["country"] or "")


def stock_for_choice(choice: str) -> int:
    info = CHOICES.get(choice) or {}
    prov = provider_for_choice(choice)
    if prov == "d1jiema":
        return 999 if d1jiema.enabled() else 0
    return stock(info.get("country") or choice)


def stock(country: str) -> int:
    prov = db.get_setting("provider", "pool")
    if prov == "nexsms_premium":
        # On-demand upstream: always allow the click, real availability is resolved at allocate time.
        return 999 + _nex_available_count(country)
    if prov != "pool":
        return 999
    r = db.q1("SELECT COUNT(*) AS n FROM numbers WHERE status='available' AND country=?", (country,))
    return int(r["n"]) if r else 0


# ------------------------------------------------------------------ parse
def parse_code(text: str) -> Optional[str]:
    s = (text or "").strip()
    if not s:
        return None
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        for p in reversed(parts):
            if re.fullmatch(r"\d{4,8}", p) and p != "0":
                return p
        # Some upstreams put a whole sentence after the separators
        # ("1|【豆包】验证码 932517，5分钟内有效"), so fall through to the regex scan.

    m = re.search(r"(?<!\d)(\d{6})(?!\d)", s)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(\d{4,8})(?!\d)", s)
    if m and m.group(1) != "0":
        return m.group(1)
    return None


def _http_get(url: str, timeout: int = 12) -> str:
    r = S.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (SMSGate)"})
    return (r.text or "").strip()


# ------------------------------------------------------------------ allocate
def allocate(country: str, card_id: int) -> dict:
    prov = db.get_setting("provider", "pool")
    if country not in COUNTRIES:
        raise ProviderError("不支持的地区")
    if prov == "pool":
        return _alloc_pool(country, card_id)
    if prov == "sms_activate":
        return _alloc_sa(country, card_id)
    if prov == "fivesim":
        return _alloc_fs(country, card_id)
    if prov == "nexsms_premium":
        return _alloc_nex(country, card_id)
    raise ProviderError("号源未配置，请联系管理员")


def _alloc_pool(country: str, card_id: int) -> dict:
    now = int(time.time())
    with db.tx() as c:
        row = c.execute(
            "SELECT * FROM numbers WHERE status='available' AND country=? ORDER BY id LIMIT 1",
            (country,)).fetchone()
        if not row:
            raise ProviderError("该地区暂无可用号码，请稍后再试")
        c.execute("UPDATE numbers SET status='allocated', card_id=?, allocated_at=? WHERE id=? AND status='available'",
                  (card_id, now, row["id"]))
        got = c.execute("SELECT * FROM numbers WHERE id=?", (row["id"],)).fetchone()
        return dict(got)


def _alloc_sa(country: str, card_id: int) -> dict:
    key = _sa_key()
    if not key:
        raise ProviderError("短信平台 API Key 未配置")
    service = db.get_setting("sms_activate_service", "go") or "go"
    cid = COUNTRIES[country]["sa"]
    try:
        text = _http_get("%s?api_key=%s&action=getNumber&service=%s&country=%d" % (
            SA_BASE, key, service, cid), timeout=25)
    except Exception as e:
        raise ProviderError("上游请求失败: %s" % type(e).__name__)
    if text.startswith("ACCESS_NUMBER:"):
        _, ext_id, phone = text.split(":", 2)
        now = int(time.time())
        with db.tx() as c:
            cur = c.execute(
                "INSERT INTO numbers(phone,country,provider,sms_url,ext_id,status,card_id,allocated_at,created_at) "
                "VALUES(?,?,?,?,?,'allocated',?,?,?)",
                (phone.strip(), country, "sms_activate", "", ext_id.strip(), card_id, now, now))
            row = c.execute("SELECT * FROM numbers WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row)
    if text.startswith("NO_NUMBERS"):
        raise ProviderError("上游暂无该地区号码")
    if text.startswith("NO_BALANCE"):
        raise ProviderError("上游余额不足")
    if text.startswith("BAD_KEY"):
        raise ProviderError("上游 API Key 无效")
    raise ProviderError("上游返回异常: %s" % text[:60])


def _alloc_fs(country: str, card_id: int) -> dict:
    key = _fs_key()
    if not key:
        raise ProviderError("5sim API Key 未配置")
    product = db.get_setting("fivesim_product", "any") or "any"
    operator = db.get_setting("fivesim_operator", "any") or "any"
    url = "%s/user/buy/activation/%s/%s/%s" % (FS_BASE, COUNTRIES[country]["fs"], operator, product)
    try:
        r = S.get(url, timeout=30, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    except Exception as e:
        raise ProviderError("上游请求失败: %s" % type(e).__name__)
    if r.status_code != 200:
        raise ProviderError("上游返回 %s: %s" % (r.status_code, (r.text or "")[:80]))
    try:
        d = r.json()
    except Exception:
        raise ProviderError("上游返回格式异常")
    phone = str(d.get("phone") or "")
    if not phone:
        raise ProviderError("上游未返回号码")
    now = int(time.time())
    price = float(d.get("price") or 0)
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO numbers(phone,country,provider,ext_id,status,card_id,allocated_at,price,created_at) "
            "VALUES(?,?,?,?,'allocated',?,?,?,?)",
            (phone, country, "fivesim", str(d.get("id") or ""), card_id, now, price, now))
        row = c.execute("SELECT * FROM numbers WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


_NEX_POOL_SQL = ("SELECT COUNT(*) AS n FROM numbers WHERE provider='nexsms_premium' "
                 "AND status='available' AND country=? AND created_at>=?")


def _nex_pool_ttl_min() -> int:
    try:
        return max(5, min(43200, int(db.get_setting("nexsms_pool_ttl_min", "1440") or 1440)))
    except Exception:
        return 1440


def _nex_batch_qty() -> int:
    try:
        return max(1, min(20, int(db.get_setting("nexsms_batch_qty", "5") or 5)))
    except Exception:
        return 5


def _nex_stock_target() -> int:
    try:
        return max(0, min(200, int(db.get_setting("nexsms_stock_target", "5") or 5)))
    except Exception:
        return 5


def _nex_buy_wait() -> float:
    try:
        return max(3.0, min(30.0, float(db.get_setting("nexsms_buy_wait_sec", "12") or 12)))
    except Exception:
        return 12.0


def _nex_available_count(country: str) -> int:
    cutoff = int(time.time()) - _nex_pool_ttl_min() * 60
    r = db.q1(_NEX_POOL_SQL, (country, cutoff))
    return int(r["n"]) if r else 0


def _take_available_nex(country: str, card_id: int):
    """Atomically hand out one pre-bought number. Returns None when the pool is empty."""
    now = int(time.time())
    cutoff = now - _nex_pool_ttl_min() * 60
    with db.tx() as c:
        row = c.execute(
            "SELECT id FROM numbers WHERE provider='nexsms_premium' AND status='available' "
            "AND country=? AND created_at>=? ORDER BY id LIMIT 1", (country, cutoff)).fetchone()
        if not row:
            return None
        cur = c.execute("UPDATE numbers SET status='allocated', card_id=?, allocated_at=? "
                        "WHERE id=? AND status='available'", (card_id, now, row["id"]))
        if cur.rowcount != 1:
            return None
        got = c.execute("SELECT * FROM numbers WHERE id=?", (row["id"],)).fetchone()
        return dict(got)


def take_ready(country: str, card_id: int):
    """Grab a pre-bought number; returns None when the pool is empty."""
    return _take_available_nex(country, card_id)


def _alloc_lock(country: str) -> threading.Lock:
    with _ALLOC_GUARD:
        lk = _ALLOC_LOCKS.get(country)
        if lk is None:
            lk = threading.Lock()
            _ALLOC_LOCKS[country] = lk
        return lk


def _waiter_add(country: str, delta: int) -> None:
    with _ALLOC_GUARD:
        _ALLOC_WAITERS[country] = max(0, _ALLOC_WAITERS.get(country, 0) + delta)


def supply_block_left() -> int:
    return max(0, int(_SUPPLY_BLOCK[0] - time.time()))


def supply_block_message() -> str:
    return _SUPPLY_BLOCK[1]


def supply_block(seconds: int, msg: str) -> None:
    _SUPPLY_BLOCK[0] = time.time() + max(1, int(seconds))
    _SUPPLY_BLOCK[1] = msg


def allocate_nex(country: str, card_id: int, wait_sec: float = 8.0):
    """Hand out one NexSMS number without putting the user in a queue.

    Fast path: a pre-bought number from the pool. Slow path: exactly one request per
    country buys a batch (cross-worker lease), the rest poll the pool for a few
    seconds. If supply is exhausted we fail fast with a retry hint instead of
    leaving the browser stuck on "排队中".
    """
    got = _take_available_nex(country, card_id)
    if got:
        _nex_after_alloc(country)
        return got

    blocked = supply_block_left()
    if blocked > 0:
        raise ProviderBusy(supply_block_message() or "号源补货中，请稍后重试",
                           retry_after=min(30, max(2, blocked)))

    lock = _alloc_lock(country)
    deadline = time.time() + max(1.0, float(wait_sec))
    _waiter_add(country, 1)
    acquired = False
    try:
        acquired = lock.acquire(timeout=max(0.2, float(wait_sec)))
        if acquired:
            got = _take_available_nex(country, card_id)
            if got:
                _nex_after_alloc(country)
                return got
            blocked = supply_block_left()
            if blocked > 0:
                raise ProviderBusy(supply_block_message() or "号源补货中，请稍后重试",
                                   retry_after=min(30, max(2, blocked)))
            tok = _nex_lease(ttl=180)
            if tok:
                try:
                    waiters = _ALLOC_WAITERS.get(country, 1)
                    qty = max(_nex_batch_qty(), min(20, waiters + 2))
                    target = _nex_stock_target()
                    if target > 0:
                        qty = max(qty, min(20, target))
                    app, items = _nex_purchase(country, min(20, qty))
                    _nex_store_items(items, country, 0, app)
                finally:
                    _nex_unlease(tok)
                got = _take_available_nex(country, card_id)
                if got:
                    _nex_after_alloc(country)
                    return got
            while time.time() < deadline:
                blocked = supply_block_left()
                if blocked > 0:
                    raise ProviderBusy(supply_block_message() or "号源补货中，请稍后重试",
                                       retry_after=min(30, max(2, blocked)))
                time.sleep(0.12)
                if _nex_available_count(country) > 0:
                    got = _take_available_nex(country, card_id)
                    if got:
                        _nex_after_alloc(country)
                        return got
            raise ProviderBusy("号源补货中，请 2 秒后重试", retry_after=2)
        while time.time() < deadline:
            blocked = supply_block_left()
            if blocked > 0:
                raise ProviderBusy(supply_block_message() or "号源补货中，请稍后重试",
                                   retry_after=min(30, max(2, blocked)))
            if _nex_available_count(country) > 0:
                got = _take_available_nex(country, card_id)
                if got:
                    _nex_after_alloc(country)
                    return got
            time.sleep(0.12)
        raise ProviderBusy("领取人数较多，号源正在补货，请 2 秒后重试", retry_after=2)
    finally:
        _waiter_add(country, -1)
        if acquired:
            try:
                lock.release()
            except Exception:
                pass


def allocate_d1(choice: str, card_type: str, card_id: int) -> dict:
    """Buy one d1jiema number inline; pacing/locking lives in d1jiema itself."""
    tok = ""
    deadline = time.time() + 6.0
    while time.time() < deadline:
        tok = d1jiema.card_lease(card_id)
        if tok:
            break
        time.sleep(0.12)
    if not tok:
        raise ProviderBusy("取号处理中，请稍候重试", retry_after=2)
    try:
        phone = d1jiema.get_phone("", card_type)
    except d1jiema.D1Busy as e:
        raise ProviderBusy(str(e), retry_after=getattr(e, "retry_after", 2))
    except d1jiema.D1Error as e:
        raise ProviderError(str(e))
    finally:
        d1jiema.card_unlease(card_id, tok)
    now = int(time.time())
    with db.tx() as c:
        if c.execute("SELECT id FROM numbers WHERE card_id=?", (card_id,)).fetchone():
            raise ProviderError("该卡密已取过号码")
        cur = c.execute(
            "INSERT INTO numbers(phone,country,provider,ext_id,choice,status,card_id,"
            "allocated_at,created_at,price,note) VALUES(?,?,?,?,?,'allocated',?,?,?,?,?)",
            (phone, "cn", "d1jiema", phone, choice, card_id, now, now, 0, card_type))
        nid = int(cur.lastrowid)
        c.execute("UPDATE card_choices SET number_id=?, alloc_at=? WHERE card_id=?",
                  (nid, now, card_id))
        return dict(c.execute("SELECT * FROM numbers WHERE id=?", (nid,)).fetchone())


def _nex_lease(ttl: int = 180, name: str = "nex_buy") -> str:
    """Cross-worker lock so two uvicorn workers never buy/prewarm at the same time."""
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


def _nex_unlease(tok: str, name: str = "nex_buy") -> None:
    if not tok:
        return
    try:
        db.conn().execute("UPDATE locks SET until=0 WHERE name=? AND owner=?", (name, tok))
    except Exception:
        pass


def _nex_extract_items(data) -> list:
    if isinstance(data, dict):
        return [x for x in (data.get("list") or []) if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _nex_purchase(country: str, qty: int):
    """Buy a batch; tries the next best app when upstream limits one project."""
    left = cooldown_left()
    if left > 0:
        raise ProviderBusy("上游限流冷却中，约 %d 秒后恢复" % left, retry_after=left)
    cands = _nex_candidates(country)
    prefix = (db.get_setting("nexsms_prefix", "") or "").strip() or None
    errors = []
    tried = 0
    for app in cands:
        if not app.get("manual") and int(app.get("quantity") or 0) <= 0:
            continue
        if tried >= 4:
            break
        tried += 1
        # Cost guard: never spend more than ~1.00 USD in one batch for a project.
        cap = 10
        try:
            price = float(app.get("price") or 0)
            if price > 0:
                cap = max(1, min(10, int(1.00 / price)))
        except Exception:
            pass
        payload = {
            "appId": int(app["appId"]),
            "type": int(app.get("type") or _nex_type()),
            "quantity": max(1, min(int(qty), cap)),
            "expiry": _nex_expiry(),
            "prefix": prefix,
            "exclude_prefix": None,
        }
        try:
            data = _nex_http("POST", "/api/premium/purchase", json=payload)
        except ProviderError as e:
            errors.append(str(e)[:140])
            continue
        items = _nex_extract_items(data)
        if items:
            cooldown_clear()
            return dict(app), items
        errors.append("上游下单成功但未返回号码")
    msg = errors[-1] if errors else "该地区暂无库存"
    low = msg.lower()
    if ("余额" in msg) or ("balance" in low) or ("insufficient" in low) or ("不足" in msg):
        supply_block(180, "号源通道余额不足，请联系管理员充值后重试")
        raise ProviderBusy("号源通道余额不足，请联系管理员充值后重试", retry_after=30)
    if ("库存" in msg) or ("stock" in low) or ("sold" in low):
        supply_block(20, "该号段暂时缺货，正在补货，请稍后重试")
        raise ProviderBusy("该号段暂时缺货，正在补货，请稍后重试", retry_after=5)
    if ("订单较多" in msg) or ("频繁" in msg) or ("稍后" in msg) or ("限" in msg):
        raise ProviderBusy("上游下单排队：%s" % msg, retry_after=15)
    raise ProviderError("NexSMS 下单失败：%s" % msg)


def _nex_store_items(items, country: str, card_id: int, app: dict) -> list:
    """Persist a batch: the first number goes to the caller, the rest become shared stock."""
    now = int(time.time())
    out = []
    with db.tx() as c:
        for idx, item in enumerate(items):
            tel = str(item.get("tel") or "").strip()
            access = item.get("smsAccess") or {}
            sms_url = str(access.get("url") or "").strip()
            if not sms_url and access.get("token"):
                sms_url = _nex_base() + "/api/number-access/sms-records?token=" + str(access.get("token"))
            if not tel or not sms_url:
                continue
            phone = tel if tel.startswith("+") else "+" + tel.lstrip("+")
            actual = _nex_phone_country(phone) or country
            allocated = bool(card_id) and idx == 0
            note = "NexSMS 优选 | %s | %s" % (item.get("appName") or app.get("name") or "豆包",
                                                app.get("cate_name") or "")
            cur = c.execute(
                "INSERT INTO numbers(phone,country,provider,sms_url,ext_id,status,card_id,allocated_at,price,note,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (phone, actual, "nexsms_premium", sms_url,
                 str(access.get("accessId") or app["appId"]),
                 "allocated" if allocated else "available",
                 card_id if allocated else 0, now if allocated else 0,
                 float(app.get("price") or 0), note, now))
            out.append(dict(c.execute("SELECT * FROM numbers WHERE id=?", (cur.lastrowid,)).fetchone()))
    return out


def _nex_after_alloc(country: str) -> None:
    """Top up only when the pool is actually empty.

    Refilling at every unit sold used to fire one tiny purchase per user, which is
    exactly what tripped upstream's per-project order limit. Now the pool is refilled
    in batch, and only after it runs dry.
    """
    try:
        if _nex_stock_target() <= 0:
            return
        if supply_block_left() > 0:
            return
        if _nex_available_count(country) >= 2:
            return
        _nex_spawn_refill(country)
    except Exception:
        pass


def _nex_spawn_refill(country: str) -> None:
    with _NEX_REFILL_LOCK:
        if country in _NEX_REFILLING:
            return
        _NEX_REFILLING.add(country)
    threading.Thread(target=_nex_refill_worker, args=(country,), daemon=True).start()


def _nex_refill_worker(country: str) -> None:
    try:
        tok = _nex_lease()
        if not tok:
            return
        try:
            need = _nex_stock_target() - _nex_available_count(country)
            if need <= 0:
                return
            qty = min(10, max(need, _nex_batch_qty()))
            app, items = _nex_purchase(country, qty)
            _nex_store_items(items, country, 0, app)
        finally:
            _nex_unlease(tok)
    except Exception:
        pass
    finally:
        with _NEX_REFILL_LOCK:
            _NEX_REFILLING.discard(country)


def refill_now(country: str) -> dict:
    """Admin-triggered top-up for one country."""
    tok = _nex_lease()
    if not tok:
        raise ProviderBusy("补货正在进行中，请稍后再试", retry_after=5)
    try:
        need = _nex_stock_target() - _nex_available_count(country)
        if need <= 0:
            need = _nex_batch_qty()
        app, items = _nex_purchase(country, max(1, min(10, max(need, _nex_batch_qty()))))
        rows = _nex_store_items(items, country, 0, app)
        return {"bought": len(rows), "available": _nex_available_count(country),
                "appId": app.get("appId"), "cate": app.get("cate_name") or "", "price": app.get("price")}
    finally:
        _nex_unlease(tok)


def warm_nex_cache() -> None:
    """Pre-scan doubao projects once per restart (only one worker wins the lease)."""
    tok = ""
    try:
        if db.get_setting("provider", "pool") != "nexsms_premium" or not _nex_key():
            return
        # Never poke a throttled upstream: each extra request can extend the ban.
        if cooldown_left() > 0:
            return
        tok = _nex_lease(ttl=120, name="nex_warm")
        if not tok:
            return
        for c in enabled_countries():
            try:
                _nex_candidates(c)
            except Exception:
                pass
            time.sleep(0.3)
    except Exception:
        pass
    finally:
        _nex_unlease(tok, "nex_warm")


def _alloc_nex(country: str, card_id: int) -> dict:
    """Serve from the pre-bought pool; buy a batch only when it runs dry.

    Waiters queue on a process lock + a DB lease, then retry the pool, so a burst of
    hundreds of users turns into a handful of batch purchases instead of hundreds.
    """
    global _NEX_WAITERS
    deadline = time.time() + _nex_buy_wait()
    last_busy = ""
    while True:
        row = _take_available_nex(country, card_id)
        if row:
            _nex_after_alloc(country)
            return row

        with _NEX_WAIT_LOCK:
            _NEX_WAITERS += 1
            waiters = _NEX_WAITERS
        try:
            left = deadline - time.time()
            if left <= 0:
                raise ProviderBusy(last_busy or "当前取号人数较多，请稍后重试", retry_after=8)
            if not _NEX_BUY_LOCK.acquire(timeout=min(left, 2.0)):
                time.sleep(0.15 + random.random() * 0.2)
                continue
            try:
                row = _take_available_nex(country, card_id)
                if row:
                    _nex_after_alloc(country)
                    return row
                tok = _nex_lease()
                if not tok:
                    time.sleep(0.2 + random.random() * 0.2)
                    continue
                rows = []
                try:
                    qty = max(_nex_batch_qty(), min(waiters, 10))
                    app, items = _nex_purchase(country, qty)
                    rows = _nex_store_items(items, country, card_id, app)
                except ProviderBusy as e:
                    last_busy = str(e)
                finally:
                    _nex_unlease(tok)
                if rows:
                    _nex_after_alloc(country)
                    return rows[0]
            finally:
                _NEX_BUY_LOCK.release()
        finally:
            with _NEX_WAIT_LOCK:
                _NEX_WAITERS -= 1
        time.sleep(0.2 + random.random() * 0.3)


# ------------------------------------------------------------------ poll
def poll(number: dict) -> dict:
    prov = number.get("provider") or "pool"
    try:
        if prov == "pool":
            url = (number.get("sms_url") or "").strip()
            if not url:
                return {"ok": False, "code": None, "raw": "号码缺少接码地址"}
            raw = _http_get(url, timeout=12)
            return {"ok": True, "code": parse_code(raw), "raw": raw[:300]}
        if prov == "sms_activate":
            return _poll_sa(number)
        if prov == "fivesim":
            return _poll_fs(number)
        if prov == "nexsms_premium":
            return _poll_nex_cached(number)
        if prov == "d1jiema":
            return _poll_d1jiema(number)
    except Exception as e:
        return {"ok": False, "code": None, "raw": "%s: %s" % (type(e).__name__, str(e)[:120])}
    return {"ok": False, "code": None, "raw": "未知号源类型"}


def _poll_sa(number: dict) -> dict:
    key = _sa_key()
    if not key:
        return {"ok": False, "code": None, "raw": "API Key 未配置"}
    text = _http_get("%s?api_key=%s&action=getStatus&id=%s" % (
        SA_BASE, key, number.get("ext_id") or ""), timeout=15)
    if text.startswith("STATUS_OK:"):
        return {"ok": True, "code": text.split(":", 1)[1].strip(), "raw": text[:200]}
    if text.startswith("STATUS_WAIT_CODE"):
        return {"ok": True, "code": None, "raw": text[:200]}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"ok": True, "code": None, "raw": text[:200]}
    return {"ok": False, "code": None, "raw": text[:200]}


def _poll_nex_cached(number: dict) -> dict:
    """Short cache + single-flight: N tabs/refreshes of one number hit upstream once."""
    key = number.get("id") or number.get("phone") or ""
    now = time.time()
    with _NEX_POLL_LOCK:
        hit = _NEX_POLL_CACHE.get(key)
        if hit and now - hit[0] < 3:
            return dict(hit[1])
        lk = _NEX_POLL_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _NEX_POLL_LOCKS[key] = lk
    with lk:
        now = time.time()
        with _NEX_POLL_LOCK:
            hit = _NEX_POLL_CACHE.get(key)
            if hit and now - hit[0] < 3:
                return dict(hit[1])
        res = _poll_nex(number)
        with _NEX_POLL_LOCK:
            if res.get("code"):
                _NEX_POLL_CACHE.pop(key, None)
                _NEX_POLL_LOCKS.pop(key, None)
            else:
                _NEX_POLL_CACHE[key] = (now, dict(res))
                if len(_NEX_POLL_CACHE) > 5000:
                    cutoff = now - 120
                    for k in [k for k, v in list(_NEX_POLL_CACHE.items()) if v[0] < cutoff]:
                        _NEX_POLL_CACHE.pop(k, None)
                        _NEX_POLL_LOCKS.pop(k, None)
        return res


def _poll_nex(number: dict) -> dict:
    url = (number.get("sms_url") or "").strip()
    if not url:
        return {"ok": False, "code": None, "raw": "NexSMS 号码缺少独立接码链接"}
    # Poll through the same channel as purchases (proxy aware), so a direct-IP
    # Cloudflare ban cannot silently break code retrieval.
    token = ""
    try:
        from urllib.parse import parse_qs, urlparse
        token = (parse_qs(urlparse(url).query).get("token") or [""])[0]
    except Exception:
        token = ""
    if token:
        try:
            data = _nex_http("GET", "/api/number-access/sms-records", params={"token": token}) or {}
        except ProviderBusy as e:
            return {"ok": False, "code": None, "raw": str(e)[:160]}
        except ProviderError as e:
            return {"ok": False, "code": None, "raw": str(e)[:160]}
        if not isinstance(data, dict):
            data = {}
    else:
        try:
            r = S.get(url, timeout=15, headers={"User-Agent": "SMSGate", "Accept": "application/json"})
        except Exception as e:
            return {"ok": False, "code": None, "raw": "NexSMS 接码请求失败: %s" % type(e).__name__}
        try:
            d = r.json()
        except Exception:
            return {"ok": False, "code": None, "raw": "NexSMS 接码返回格式异常（HTTP %d）" % r.status_code}
        try:
            code_no = int(d.get("code") if d.get("code") is not None else 0)
        except Exception:
            code_no = -1
        if r.status_code != 200 or code_no != 0:
            return {"ok": False, "code": None,
                    "raw": "NexSMS: %s" % str(d.get("message") or d.get("errorCode") or r.status_code)[:100]}
        data = d.get("data") or {}
    records = data.get("records") if isinstance(data, dict) else None
    if records is None and isinstance(data, dict):
        records = data.get("list") or []
    records = records or []
    prefix = _nex_sms_prefix()
    for rec in records:
        content = str(rec.get("content") or rec.get("sms") or "")
        if not _nex_prefix_ok(content, prefix):
            continue
        val = str(rec.get("code") or "").strip()
        if not re.fullmatch(r"[0-9]{4,8}", val):
            val = parse_code(content) or ""
        if val:
            return {"ok": True, "code": val, "raw": content[:300]}
        return {"ok": True, "code": None, "raw": "已收到 %s 短信，但未解析到验证码" % prefix}
    if records:
        return {"ok": True, "code": None,
                "raw": "收到 %d 条短信，均不以 %s 开头" % (len(records), prefix)}
    return {"ok": True, "code": None, "raw": "等待 %s 短信" % prefix}


def _poll_fs(number: dict) -> dict:
    key = _fs_key()
    if not key:
        return {"ok": False, "code": None, "raw": "API Key 未配置"}
    url = "%s/user/check/%s" % (FS_BASE, number.get("ext_id") or "")
    r = S.get(url, timeout=15, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    if r.status_code != 200:
        return {"ok": False, "code": None, "raw": "上游 %s" % r.status_code}
    try:
        d = r.json()
    except Exception:
        return {"ok": False, "code": None, "raw": "返回格式异常"}
    sms = d.get("sms") or []
    if sms:
        code = parse_code(str(sms[-1].get("code") or "") or str(sms[-1].get("text") or ""))
        if code:
            return {"ok": True, "code": code, "raw": str(sms[-1].get("text") or code)[:300]}
    status = str(d.get("status") or "")
    if status in ("CANCELED", "BANNED", "TIMEOUT"):
        return {"ok": False, "code": None, "raw": "上游状态 " + status}
    return {"ok": True, "code": None, "raw": status[:120]}


_D1_POLL_CACHE = {}
_D1_POLL_LOCK = threading.Lock()


def _d1_parse(raw: str) -> Optional[str]:
    """Only accept a SMS that really belongs to the configured project keyword."""
    text = (raw or "").strip()
    if not text or "尚未收到" in text:
        return None
    kw = d1jiema.keyword()
    if kw and kw not in text:
        return None
    pref = d1jiema.sms_prefix()
    if pref and not _nex_prefix_ok(text, pref) and kw not in text:
        return None
    return parse_code(text)


def _poll_d1jiema(number: dict) -> dict:
    phone = str(number.get("phone") or number.get("ext_id") or "")
    if not phone:
        return {"ok": False, "code": None, "raw": "号码缺失"}
    now = time.time()
    with _D1_POLL_LOCK:
        hit = _D1_POLL_CACHE.get(phone)
        if hit and now - hit[0] < 2.5:
            return dict(hit[1])
        if len(_D1_POLL_CACHE) > 5000:
            _D1_POLL_CACHE.clear()
    tok = d1jiema.poll_lease(phone)
    if not tok:
        # Another worker / tab is already asking upstream for this number.
        return {"ok": True, "code": None, "raw": "取码中"}
    try:
        raw = d1jiema.get_msg(phone)
        code = _d1_parse(raw)
        out = {"ok": True, "code": code, "raw": (raw or "")[:300]}
    except d1jiema.D1Busy as e:
        out = {"ok": True, "code": None, "raw": str(e)[:200]}
    except d1jiema.D1Error as e:
        out = {"ok": False, "code": None, "raw": str(e)[:200]}
    except Exception as e:
        out = {"ok": False, "code": None, "raw": "%s: %s" % (type(e).__name__, str(e)[:120])}
    finally:
        d1jiema.poll_unlease(phone, tok)
    with _D1_POLL_LOCK:
        _D1_POLL_CACHE[phone] = (time.time(), dict(out))
    return out


def finish(number: dict, success: bool) -> None:
    prov = number.get("provider") or "pool"
    try:
        if prov == "sms_activate":
            key = _sa_key()
            if key:
                _http_get("%s?api_key=%s&action=setStatus&status=%d&id=%s" % (
                    SA_BASE, key, 6 if success else 8, number.get("ext_id") or ""), timeout=15)
        elif prov == "fivesim":
            key = _fs_key()
            if key:
                S.get("%s/user/%s/%s" % (FS_BASE, "finish" if success else "cancel", number.get("ext_id") or ""),
                      timeout=15, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        elif prov == "nexsms_premium":
            # NexSMS premium numbers have no documented cancel endpoint; let them expire upstream.
            return
        elif prov == "d1jiema":
            # d1jiema auto-releases / auto-blocks numbers; nothing to call.
            return
    except Exception:
        pass


def test_provider(prov: str = "") -> dict:
    prov = (prov or "").strip() or db.get_setting("provider", "pool")
    if prov == "d1jiema":
        return d1jiema.test()
    if prov == "pool":
        n = db.q1("SELECT COUNT(*) AS n FROM numbers WHERE status='available'")
        return {"ok": True, "message": "本地号池可用号码 %d 个" % (int(n["n"]) if n else 0)}
    if prov == "sms_activate":
        key = _sa_key()
        if not key:
            return {"ok": False, "message": "API Key 未配置"}
        try:
            text = _http_get("%s?api_key=%s&action=getBalance" % (SA_BASE, key), timeout=15)
            return {"ok": text.startswith("ACCESS_BALANCE"), "message": "上游返回: " + text[:80]}
        except Exception as e:
            return {"ok": False, "message": "请求失败: %s" % type(e).__name__}
    if prov == "fivesim":
        key = _fs_key()
        if not key:
            return {"ok": False, "message": "API Key 未配置"}
        try:
            r = S.get(FS_BASE + "/user/profile", timeout=15,
                      headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
            return {"ok": r.status_code == 200, "message": "上游返回 %s" % r.status_code}
        except Exception as e:
            return {"ok": False, "message": "请求失败: %s" % type(e).__name__}
    if prov == "nexsms_premium":
        if not _nex_key():
            return {"ok": False, "message": "NexSMS API Key 未配置"}
        cd = cooldown_left()
        if cd > 0:
            return {"ok": False, "message": "上游通道限流中，约 %d 分钟后恢复（未发起请求）" % max(1, cd // 60)}
        try:
            bal = _nex_http("GET", "/api/balance") or {}
            app = _nex_find_app("us")
            return {"ok": True, "message": "NexSMS 连接正常，余额 %s USD；%s appId=%s（%s），类型=%s，库存=%s，单价=%s，成功率=%s%%" % (
                bal.get("balance", "?"), app.get("name", "豆包"), app.get("appId"),
                app.get("cate_name") or "自动", app.get("type"), app.get("quantity"),
                app.get("price"), app.get("success"))}
        except ProviderError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            return {"ok": False, "message": "NexSMS 测试失败: %s" % type(e).__name__}
    return {"ok": False, "message": "未知号源"}