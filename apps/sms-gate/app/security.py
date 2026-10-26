# -*- coding: utf-8 -*-
"""Password hashing, TOTP, sessions, CSRF, rate limiting, IP bans, audit."""
from __future__ import annotations
import base64, hashlib, hmac, ipaddress, json, os, secrets, struct, threading, time
from typing import Optional

from fastapi import HTTPException, Request

from . import config, db

# ---------------------------------------------------------------- passwords
PW_ITER = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), PW_ITER)
    return "pbkdf2_sha256$%d$%s$%s" % (PW_ITER, salt, dk.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


_DUMMY = {"h": ""}


def dummy_verify(password: str) -> bool:
    """Burn the same PBKDF2 cost for an unknown username (no timing oracle)."""
    if not _DUMMY["h"]:
        _DUMMY["h"] = hash_password("dummy-password-for-timing")
    return verify_password(password, _DUMMY["h"])


def guard_check(key: str) -> tuple[bool, int]:
    """Credential guard: returns (allowed, seconds_left)."""
    try:
        r = db.q1("SELECT locked_until, fails FROM login_guard WHERE key=?", (key,))
    except Exception:
        return True, 0
    if not r:
        return True, 0
    now = int(time.time())
    left = int(r["locked_until"] or 0) - now
    return (left <= 0), max(0, left)


def guard_fail(key: str, threshold: int = 5, window: int = 900, base_lock: int = 300) -> int:
    """Count a bad credential; lock with exponential backoff. Returns lock seconds."""
    now = int(time.time())
    try:
        with db.tx() as c:
            r = c.execute("SELECT * FROM login_guard WHERE key=?", (key,)).fetchone()
            if not r:
                c.execute("INSERT INTO login_guard(key,fails,first_fail,last_fail,locked_until) "
                          "VALUES(?,?,?,?,0)", (key, 1, now, now))
                return 0
            fails = int(r["fails"] or 0)
            first = int(r["first_fail"] or 0)
            if now - first > window:
                fails, first = 1, now
            else:
                fails += 1
            if fails >= threshold:
                lock = min(86400, base_lock * (2 ** min(6, fails - threshold)))
                c.execute("UPDATE login_guard SET fails=?, first_fail=?, last_fail=?, locked_until=? "
                          "WHERE key=?", (fails, first, now, now + lock, key))
                return lock
            c.execute("UPDATE login_guard SET fails=?, first_fail=?, last_fail=? WHERE key=?",
                      (fails, first, now, key))
            return 0
    except Exception:
        return 0


def guard_ok(key: str) -> None:
    try:
        db.conn().execute("DELETE FROM login_guard WHERE key=?", (key,))
    except Exception:
        pass


def admin_ip_allowed(ip: str) -> bool:
    """Optional allowlist for the admin panel (`admin_ip_allow`, comma separated)."""
    raw = (db.get_setting("admin_ip_allow", "") or "").strip()
    if not raw:
        return True
    try:
        import ipaddress
        addr = ipaddress.ip_address(ip)
    except Exception:
        return False
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "/" in part:
                if ipaddress.ip_address(ip) in ipaddress.ip_network(part, strict=False):
                    return True
            elif part == ip:
                return True
        except Exception:
            continue
    return False


def password_problem(pw: str) -> Optional[str]:
    if len(pw) < 10:
        return "密码至少 10 位"
    if not any(c.islower() for c in pw) or not any(c.isupper() for c in pw):
        return "密码需同时包含大小写字母"
    if not any(c.isdigit() for c in pw):
        return "密码需包含数字"
    return None


# ---------------------------------------------------------------- TOTP
B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def new_totp_secret(nbytes: int = 20) -> str:
    raw = secrets.token_bytes(nbytes)
    out = []
    bits = 0
    val = 0
    for b in raw:
        val = (val << 8) | b
        bits += 8
        while bits >= 5:
            out.append(B32[(val >> (bits - 5)) & 31])
            bits -= 5
    return "".join(out)


def _b32decode(secret: str) -> bytes:
    secret = secret.strip().replace(" ", "").upper().rstrip("=")
    buf, bits, val = bytearray(), 0, 0
    for ch in secret:
        idx = B32.find(ch)
        if idx < 0:
            raise ValueError("bad base32")
        val = (val << 5) | idx
        bits += 5
        if bits >= 8:
            buf.append((val >> (bits - 8)) & 255)
            bits -= 8
    return bytes(buf)


def totp_at(secret: str, ts: Optional[int] = None, period: int = 30, digits: int = 6) -> str:
    if ts is None:
        ts = int(time.time())
    counter = int(ts) // period
    key = _b32decode(secret)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    now = int(time.time())
    for i in range(-window, window + 1):
        try:
            if hmac.compare_digest(totp_at(secret, now + i * 30), code):
                return True
        except Exception:
            return False
    return False


def otpauth_uri(username: str, secret: str) -> str:
    site = db.get_setting("site_name", config.SITE_NAME) or config.SITE_NAME
    label = "%s:%s" % (site.replace(":", " "), username)
    from urllib.parse import quote
    return "otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=6&period=30" % (
        quote(label), secret, quote(site))


def qr_svg(data: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=8, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        import io
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return ""


# ---------------------------------------------------------------- encryption at rest
def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + struct.pack(">I", counter)).digest()
        counter += 1
    return bytes(out[:n])


def enc(text: str) -> str:
    if not text:
        return ""
    try:
        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(config.FERNET_KEY.encode()).digest())
        return "f1:" + Fernet(key).encrypt(text.encode()).decode()
    except Exception:
        key = hashlib.sha256(("k:" + config.FERNET_KEY).encode()).digest()
        nonce = secrets.token_bytes(12)
        data = text.encode()
        ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
        return "x1:" + base64.urlsafe_b64encode(nonce + ct).decode()


def dec(blob: str) -> str:
    if not blob:
        return ""
    try:
        if blob.startswith("f1:"):
            from cryptography.fernet import Fernet
            key = base64.urlsafe_b64encode(hashlib.sha256(config.FERNET_KEY.encode()).digest())
            return Fernet(key).decrypt(blob[3:].encode()).decode()
        if blob.startswith("x1:"):
            raw = base64.urlsafe_b64decode(blob[3:].encode())
            nonce, ct = raw[:12], raw[12:]
            key = hashlib.sha256(("k:" + config.FERNET_KEY).encode()).digest()
            return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct)))).decode()
    except Exception:
        return ""
    return ""


# ---------------------------------------------------------------- request meta
def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    fwd = request.headers.get("x-real-ip") or ""
    if not fwd:
        xff = request.headers.get("x-forwarded-for") or ""
        fwd = xff.split(",")[0].strip() if xff else ""
    if peer in (config.TRUSTED_PROXY, "::1", "localhost"):
        return fwd or peer
    return peer


def ua(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:300]


def is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") or "").lower() == "https"


# ---------------------------------------------------------------- rate limiting
# In-process fixed-window counters. The old implementation opened a write
# transaction on every request, which serialised the whole site on SQLite under
# load; counters are per worker now (2 workers => up to 2x the configured rate).
_RL = {}
_RL_LOCK = threading.Lock()
_RL_MAX = 50000


def hit(bucket: str, limit: int, window: int) -> tuple[bool, int]:
    """Fixed-window counter. Returns (allowed, retry_after)."""
    now = int(time.time())
    with _RL_LOCK:
        if len(_RL) > _RL_MAX:
            cutoff = now - 3600
            for k, v in list(_RL.items()):
                if v[0] < cutoff:
                    _RL.pop(k, None)
        start, cnt = _RL.get(bucket, (now, 0))
        if now - start >= window:
            _RL[bucket] = (now, 1)
            return True, 0
        if cnt >= limit:
            return False, max(1, window - (now - start))
        _RL[bucket] = (start, cnt + 1)
        return True, 0


def hit_shared(bucket: str, limit: int, window: int) -> tuple[bool, int]:
    """Cross-worker fixed-window counter (SQLite), for public relay endpoints.

    The in-process `hit()` is per uvicorn worker, so 4 workers effectively allow
    4x the configured limit. Relay traffic is low volume, so paying one small
    transaction per request keeps the advertised limit exact.
    """
    now = int(time.time())
    try:
        with db.tx() as c:
            row = c.execute("SELECT start, cnt FROM rl WHERE bucket=?", (bucket,)).fetchone()
            if not row or now - int(row["start"]) >= window:
                c.execute("INSERT INTO rl(bucket,start,cnt) VALUES(?,?,1) "
                          "ON CONFLICT(bucket) DO UPDATE SET start=excluded.start, cnt=1",
                          (bucket, now))
                if now % 97 == 0:
                    c.execute("DELETE FROM rl WHERE start<?", (now - 3600,))
                return True, 0
            if int(row["cnt"]) >= limit:
                return False, max(1, window - (now - int(row["start"])))
            c.execute("UPDATE rl SET cnt=cnt+1 WHERE bucket=?", (bucket,))
            return True, 0
    except Exception:
        # Never let limiter trouble break the customer-facing endpoint.
        return True, 0


# Write-behind guards: last_seen/IP counters do not need a row write per request.
_SEEN = {}
_SEEN_LOCK = threading.Lock()


def _seen_due(key: str, every: int = 60) -> bool:
    now = time.time()
    with _SEEN_LOCK:
        last = _SEEN.get(key, 0)
        if now - last < every:
            return False
        _SEEN[key] = now
        if len(_SEEN) > 50000:
            cutoff = now - 3600
            for k, v in list(_SEEN.items()):
                if v < cutoff:
                    _SEEN.pop(k, None)
        return True


# ---------------------------------------------------------------- IP ledger / bans
def touch_ip(ip: str, failed: bool = False) -> None:
    if not ip:
        return
    if not failed and not _seen_due("ip:" + ip, 30):
        return
    now = int(time.time())
    try:
        db.conn().execute(
            "INSERT INTO ips(ip,first_seen,last_seen,req_count,fail_count) VALUES(?,?,?,1,?) "
            "ON CONFLICT(ip) DO UPDATE SET last_seen=excluded.last_seen, req_count=req_count+1, "
            "fail_count=fail_count+excluded.fail_count",
            (ip, now, now, 1 if failed else 0))
    except Exception:
        pass


def blocked_until(ip: str) -> tuple[int, str]:
    now = int(time.time())
    try:
        rows = db.q("SELECT ip,blocked_until,block_reason FROM ips WHERE blocked_until>?", (now,))
    except Exception:
        return 0, ""
    try:
        addr = ipaddress.ip_address(ip)
    except Exception:
        return 0, ""
    for r in rows:
        try:
            raw = r["ip"]
            net = ipaddress.ip_network(raw, strict=False) if "/" in raw else ipaddress.ip_network(raw + "/32")
            if addr in net:
                return int(r["blocked_until"]), r["block_reason"] or "auto"
        except Exception:
            continue
    return 0, ""


def ban_ip(ip: str, seconds: int, reason: str) -> None:
    if not ip:
        return
    now = int(time.time())
    until = now + seconds if seconds > 0 else 0
    try:
        db.conn().execute(
            "INSERT INTO ips(ip,first_seen,last_seen,req_count,fail_count,blocked_until,block_reason) "
            "VALUES(?,?,?,0,0,?,?) ON CONFLICT(ip) DO UPDATE SET blocked_until=excluded.blocked_until,"
            "block_reason=excluded.block_reason,last_seen=excluded.last_seen",
            (ip, now, now, until, reason[:200]))
        audit("ip_ban", "alert", ip=ip, detail={"seconds": seconds, "reason": reason})
    except Exception:
        pass


def unban_ip(ip: str) -> None:
    db.conn().execute("UPDATE ips SET blocked_until=0, block_reason='' WHERE ip=?", (ip,))


def record_failure(ip: str, kind: str, threshold: int = 5, window: int = 600,
                   ban_seconds: int = 1800, actor: str = "", detail: str = "") -> bool:
    """Register one failed attempt and auto-ban on threshold.

    The audit row is written here *before* counting, so the current failure is
    included and the Nth bad attempt bans immediately instead of the N+1th.
    """
    if not ip:
        return False
    now = int(time.time())
    touch_ip(ip, failed=True)
    audit(kind, "warn", ip=ip, actor=actor, detail=detail)
    if db.get_setting("auto_ban_enabled", "1") != "1":
        return False
    n = db.q1("SELECT COUNT(*) AS n FROM events WHERE kind=? AND ip=? AND ts>?",
              (kind, ip, now - window))
    total = db.q1("SELECT COUNT(*) AS n FROM events WHERE kind=? AND ip=? AND ts>?",
                  (kind, ip, now - 3600))
    if (n and n["n"] >= threshold) or (total and total["n"] >= threshold * 4):
        ban_ip(ip, ban_seconds, "auto:" + kind)
        return True
    return False


def failure_count(ip: str, kind: str, window: int = 600) -> int:
    r = db.q1("SELECT COUNT(*) AS n FROM events WHERE kind=? AND ip=? AND ts>?",
              (kind, ip, int(time.time()) - window))
    return int(r["n"]) if r else 0


# ---------------------------------------------------------------- audit
def audit(kind: str, level: str = "info", ip: str = "", actor: str = "", card_id: int = 0,
          detail: object = None) -> None:
    try:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        db.conn().execute(
            "INSERT INTO events(ts,kind,level,ip,actor,card_id,detail) VALUES(?,?,?,?,?,?,?)",
            (int(time.time()), kind, level, ip or "", actor or "", int(card_id or 0), (detail or "")[:2000]))
    except Exception:
        pass


# ---------------------------------------------------------------- sessions
def _new_sid() -> str:
    return secrets.token_urlsafe(32)


def create_admin_session(admin_id: int, ip: str, ua_str: str) -> tuple[str, str]:
    sid, csrf = _new_sid(), _new_sid()[:32]
    now = int(time.time())
    db.conn().execute(
        "INSERT INTO admin_sessions(sid,admin_id,csrf,ip,ua,created_at,expires_at,last_seen) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (sid, admin_id, csrf, ip, ua_str, now, now + config.ADMIN_SESSION_TTL, now))
    return sid, csrf


def create_card_session(card_id: int, ip: str, ua_str: str) -> tuple[str, str]:
    sid, csrf = _new_sid(), _new_sid()[:32]
    now = int(time.time())
    db.conn().execute(
        "INSERT INTO card_sessions(sid,card_id,csrf,ip,ua,created_at,expires_at,last_seen) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (sid, card_id, csrf, ip, ua_str, now, now + config.CARD_SESSION_TTL, now))
    return sid, csrf


def get_admin_session(request: Request) -> Optional[dict]:
    sid = request.cookies.get(config.ADMIN_COOKIE) or ""
    if not sid:
        return None
    now = int(time.time())
    r = db.q1(
        "SELECT s.sid,s.csrf,s.expires_at,s.admin_id,s.ip,s.stage,a.username FROM admin_sessions s "
        "JOIN admins a ON a.id=s.admin_id WHERE s.sid=? AND s.expires_at>?", (sid, now))
    if not r:
        return None
    if _seen_due("as:" + sid, 60):
        db.conn().execute("UPDATE admin_sessions SET last_seen=? WHERE sid=?", (now, sid))
    return dict(r)


def get_card_session(request: Request) -> Optional[dict]:
    sid = request.cookies.get(config.CARD_COOKIE) or ""
    if not sid:
        return None
    now = int(time.time())
    r = db.q1(
        "SELECT s.sid,s.csrf,s.card_id,s.ip,c.key_prefix,c.status,c.used_codes,c.max_codes "
        "FROM card_sessions s JOIN cards c ON c.id=s.card_id WHERE s.sid=? AND s.expires_at>?",
        (sid, now))
    if not r:
        return None
    if _seen_due("cs:" + sid, 60):
        db.conn().execute("UPDATE card_sessions SET last_seen=? WHERE sid=?", (now, sid))
    return dict(r)


def require_admin(request: Request) -> dict:
    s = get_admin_session(request)
    if not s:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return s


def require_csrf(request: Request, token: str, expected: str) -> None:
    if not expected or not hmac.compare_digest(str(token or ""), str(expected)):
        raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面")


def purge_expired() -> None:
    now = int(time.time())
    c = db.conn()
    c.execute("DELETE FROM admin_sessions WHERE expires_at<?", (now,))
    c.execute("DELETE FROM card_sessions WHERE expires_at<?", (now,))
    c.execute("DELETE FROM rate_limits WHERE window_start<?", (now - 6 * 3600,))