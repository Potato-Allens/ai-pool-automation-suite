# -*- coding: utf-8 -*-
"""Admin API: auth (password + TOTP), cards, numbers, records, audit, IP control."""
from __future__ import annotations
import asyncio, hashlib, hmac, json, random, secrets, string, time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config, db, nexq, providers, security

router = APIRouter()

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def ok(**kw):
    kw.setdefault("ok", True)
    return JSONResponse(kw)


def err(msg: str, code: int = 400, **kw):
    kw.update({"ok": False, "error": msg})
    return JSONResponse(kw, status_code=code)


def _cookie(resp: JSONResponse, request: Request, sid: str, max_age: int) -> None:
    resp.set_cookie(config.ADMIN_COOKIE, sid, max_age=max_age, httponly=True,
                    samesite="lax", secure=security.is_https(request), path="/")


def _admin(request: Request) -> dict:
    s = security.get_admin_session(request)
    if not s or s.get("stage") != "active":
        raise HTTPException(status_code=401, detail="未登录")
    return s


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


def _csrf(request: Request, sess: dict, body: dict) -> None:
    security.require_csrf(request, request.headers.get("x-csrf", "") or body.get("csrf", ""), sess["csrf"])


def _admin_row(admin_id: int):
    return db.q1("SELECT * FROM admins WHERE id=?", (admin_id,))


def _require_ready(admin) -> None:
    if not admin or not admin["setup_done"]:
        raise HTTPException(status_code=428, detail="后台尚未初始化")


# ------------------------------------------------------------------ auth
@router.get("/api/bootstrap")
def bootstrap(request: Request):
    row = db.q1("SELECT COUNT(*) AS n FROM admins")
    has_admin = bool(row and row["n"])
    pending = bool(db.q1("SELECT id FROM admins WHERE totp_on=0 LIMIT 1"))
    s = security.get_admin_session(request)
    return {"ok": True, "needs_setup": not has_admin,
            "setup_pending": pending,
            "logged_in": bool(s and s.get("stage") == "active"),
            "stage": (s or {}).get("stage", ""),
            "username": (s or {}).get("username", ""),
            "site_name": db.get_setting("site_name", config.SITE_NAME)}


@router.post("/api/setup")
async def setup(request: Request):
    ip = security.client_ip(request)
    allowed, retry = security.hit("setup:%s" % ip, 6, 900)
    if not allowed:
        return err("尝试过于频繁，请 %d 秒后再试" % retry, 429)
    if db.q1("SELECT COUNT(*) AS n FROM admins")["n"]:
        return err("后台已初始化", 409)
    body = await _body(request)
    token = str(body.get("token") or "").strip()
    if not token or not hmac.compare_digest(token, config.SETUP_TOKEN):
        security.record_failure(ip, "setup_fail", threshold=5, ban_seconds=3600)
        return err("初始化令牌不正确", 403)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not (3 <= len(username) <= 32) or not username.isascii():
        return err("用户名需为 3-32 位英文/数字")
    problem = security.password_problem(password)
    if problem:
        return err(problem)

    secret = security.new_totp_secret()
    now = int(time.time())
    try:
        with db.tx() as c:
            cur = c.execute(
                "INSERT INTO admins(username,pass_hash,totp_secret,totp_on,pending_secret,setup_done,created_at) "
                "VALUES(?,?,'',0,?,0,?)",
                (username, security.hash_password(password), security.enc(secret), now))
            admin_id = cur.lastrowid
    except Exception:
        return err("创建失败，请重试", 500)

    sid, csrf = security.create_admin_session(admin_id, ip, security.ua(request))
    db.conn().execute("UPDATE admin_sessions SET stage='setup' WHERE sid=?", (sid,))
    uri = security.otpauth_uri(username, secret)
    security.audit("admin_setup", "alert", ip=ip, actor=username, detail="admin created")
    resp = ok(csrf=csrf, otpauth=uri, qr_svg=security.qr_svg(uri), username=username)
    _cookie(resp, request, sid, 1800)
    return resp


@router.get("/api/setup/pending")
def setup_pending(request: Request):
    """Return the QR for a setup session that was created but not yet bound."""
    s = security.get_admin_session(request)
    if not s or s.get("stage") != "setup":
        return err("初始化会话已过期，请重新开始", 401)
    admin = _admin_row(s["admin_id"])
    if not admin or admin["totp_on"]:
        return err("后台已完成初始化", 409)
    if not admin["pending_secret"]:
        return err("没有待绑定的验证器，请重新开始", 409)
    secret = security.dec(admin["pending_secret"])
    uri = security.otpauth_uri(admin["username"], secret)
    return ok(csrf=s["csrf"], otpauth=uri, qr_svg=security.qr_svg(uri),
              username=admin["username"])


@router.post("/api/setup/resume")
async def setup_resume(request: Request):
    """Recover an interrupted first-run setup (admin exists, TOTP not bound)."""
    ip = security.client_ip(request)
    allowed, retry = security.hit("setup_resume:%s" % ip, 6, 900)
    if not allowed:
        return err("尝试过于频繁，请 %d 秒后再试" % retry, 429)
    body = await _body(request)
    token = str(body.get("token") or "").strip()
    if not token or not hmac.compare_digest(token, config.SETUP_TOKEN):
        security.record_failure(ip, "setup_resume_fail", threshold=5, ban_seconds=3600)
        return err("初始化令牌不正确", 403)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    admin = db.q1("SELECT * FROM admins WHERE username=?", (username,))
    if not admin or admin["totp_on"]:
        return err("没有待完成的初始化", 409)
    if not security.verify_password(password, admin["pass_hash"]):
        security.record_failure(ip, "setup_resume_fail", threshold=6, ban_seconds=1800)
        security.audit("admin_setup_resume_fail", "warn", ip=ip, actor=username,
                       detail="bad password")
        return err("用户名或密码错误", 403)

    ukey = "u:" + str(admin["username"]).lower()
    secret = security.new_totp_secret()
    with db.tx() as c:
        c.execute("UPDATE admins SET pending_secret=?, setup_done=0 WHERE id=?",
                  (security.enc(secret), admin["id"]))
        c.execute("DELETE FROM admin_sessions WHERE admin_id=? AND stage='setup'", (admin["id"],))
    security.guard_ok(ukey)
    security.guard_ok("ip:" + ip)
    sid, csrf = security.create_admin_session(admin["id"], ip, security.ua(request))
    db.conn().execute("UPDATE admin_sessions SET stage='setup' WHERE sid=?", (sid,))
    uri = security.otpauth_uri(admin["username"], secret)
    security.audit("admin_setup_resume", "alert", ip=ip, actor=admin["username"],
                   detail="resume pending setup")
    resp = ok(csrf=csrf, otpauth=uri, qr_svg=security.qr_svg(uri), username=admin["username"])
    _cookie(resp, request, sid, 1800)
    return resp


@router.post("/api/setup/verify")
async def setup_verify(request: Request):
    s = security.get_admin_session(request)
    if not s or s.get("stage") != "setup":
        return err("初始化会话已过期，请重新开始", 401)
    ip = security.client_ip(request)
    body = await _body(request)
    security.require_csrf(request, request.headers.get("x-csrf", ""), s["csrf"])
    admin = _admin_row(s["admin_id"])
    secret = security.dec(admin["pending_secret"])
    code = str(body.get("code") or "").strip()
    allowed, retry = security.hit("setupverify:%s" % ip, 10, 600)
    if not allowed:
        return err("尝试过于频繁，请 %d 秒后再试" % retry, 429)
    if not security.verify_totp(secret, code):
        security.record_failure(ip, "setup_totp_fail", threshold=6, ban_seconds=1800)
        return err("验证码不正确，请确认手机时间准确后重试", 403)
    db.conn().execute(
        "UPDATE admins SET totp_secret=?, totp_on=1, pending_secret='', setup_done=1 WHERE id=?",
        (security.enc(secret), admin["id"]))
    db.conn().execute("UPDATE admin_sessions SET stage='active' WHERE sid=?", (s["sid"],))
    security.audit("totp_bound", "alert", ip=ip, actor=admin["username"], detail="google authenticator bound")
    return ok(username=admin["username"], csrf=s["csrf"])


@router.post("/api/login")
async def login(request: Request):
    ip = security.client_ip(request)
    if not security.admin_ip_allowed(ip):
        security.audit("admin_ip_denied", "alert", ip=ip, detail="not in allowlist")
        return err("当前 IP 不允许登录后台", 403)
    allowed, retry = security.hit("login:%s" % ip, 10, 600)
    if not allowed:
        return err("尝试过于频繁，请 %d 秒后再试" % retry, 429, retry_after=retry)
    body = await _body(request)
    username = str(body.get("username") or "").strip()[:64]
    password = str(body.get("password") or "")[:256]
    ukey = "u:" + username.lower()
    # Credential stuffing: lock the account name and the source IP independently.
    for gk in (ukey, "ip:" + ip):
        okg, wait = security.guard_check(gk)
        if not okg:
            security.audit("admin_login_locked", "alert", ip=ip, actor=username,
                           detail={"key": gk, "wait": wait})
            return err("尝试过多，请 %d 分钟后再试" % max(1, wait // 60 + 1), 429, retry_after=wait)
    admin = db.q1("SELECT * FROM admins WHERE username=?", (username,))
    if not admin:
        # Same PBKDF2 cost as a real check: no username enumeration by timing.
        security.dummy_verify(password)
        bad = True
    else:
        bad = not security.verify_password(password, admin["pass_hash"])
    if bad:
        banned = security.record_failure(ip, "admin_login_fail", threshold=5, ban_seconds=1800,
                                          actor=username, detail="bad credentials")
        lock_u = security.guard_fail(ukey, threshold=5, window=900, base_lock=300)
        lock_ip = security.guard_fail("ip:" + ip, threshold=10, window=900, base_lock=600)
        # Slow down automated guessing; keeps the response shape identical.
        await asyncio.sleep(0.25 + random.random() * 0.35)
        lock = max(lock_u, lock_ip)
        msg = "用户名或密码错误"
        if lock:
            msg += "，已锁定 %d 分钟" % max(1, lock // 60)
        elif banned:
            msg += "，IP 已临时封禁"
        return err(msg, 401, retry_after=lock or None)
    if not admin["setup_done"]:
        return err("后台未完成初始化", 428)
    if not admin["totp_on"]:
        return err("尚未绑定谷歌验证器，请完成初始化", 428)

    sid, csrf = security.create_admin_session(admin["id"], ip, security.ua(request))
    db.conn().execute("UPDATE admin_sessions SET stage='totp_pending' WHERE sid=?", (sid,))
    security.audit("admin_login_step1", "info", ip=ip, actor=username)
    resp = ok(need_totp=True, csrf=csrf, username=username)
    _cookie(resp, request, sid, 600)
    return resp


@router.post("/api/login/code")
async def login_code(request: Request):
    """Code-only login: the TOTP itself is the sole factor.

    Deliberately strict on the guessing side, because a 6-digit secret with no
    password in front of it is the whole security boundary.
    """
    ip = security.client_ip(request)
    if not security.admin_ip_allowed(ip):
        security.audit("admin_ip_denied", "alert", ip=ip, detail="not in allowlist")
        return err("当前 IP 不允许登录后台", 403)
    allowed, retry = security.hit("logincode:%s" % ip, 10, 600)
    if not allowed:
        return err("尝试过于频繁，请 %d 秒后再试" % retry, 429, retry_after=retry)
    body = await _body(request)
    code = str(body.get("code") or "").strip()[:8]
    if len(code) != 6 or not code.isdigit():
        return err("请输入 6 位动态验证码", 400)
    okg, wait = security.guard_check("ip:" + ip)
    if not okg:
        security.audit("admin_code_locked", "alert", ip=ip, detail={"wait": wait})
        return err("尝试过多，请 %d 分钟后再试" % max(1, wait // 60 + 1), 429, retry_after=wait)
    rows = [dict(r) for r in db.q("SELECT * FROM admins WHERE setup_done=1 AND totp_on=1 ORDER BY id")]
    if not rows:
        return err("后台尚未完成初始化", 428)
    admin = None
    for r in rows:
        try:
            if security.verify_totp(security.dec(r["totp_secret"]), code):
                admin = r
                break
        except Exception:
            continue
    if not admin:
        banned = security.record_failure(ip, "admin_totp_fail", threshold=6, ban_seconds=3600,
                                          actor="", detail="bad totp")
        lock_ip = security.guard_fail("ip:" + ip, threshold=5, window=600, base_lock=600)
        security.audit("admin_code_fail", "warn", ip=ip, detail="bad totp (code-only login)")
        await asyncio.sleep(0.3 + random.random() * 0.4)
        msg = "动态验证码错误"
        if lock_ip:
            msg += "，已锁定 %d 分钟" % max(1, lock_ip // 60)
        elif banned:
            msg += "，IP 已临时封禁"
        return err(msg, 401, retry_after=lock_ip or None)
    security.guard_ok("ip:" + ip)
    sid, csrf = security.create_admin_session(admin["id"], ip, security.ua(request))
    db.conn().execute("UPDATE admin_sessions SET stage='active' WHERE sid=?", (sid,))
    db.conn().execute("UPDATE admins SET last_login_at=?, last_login_ip=? WHERE id=?",
                      (int(time.time()), ip, admin["id"]))
    security.audit("admin_login", "alert", ip=ip, actor=admin["username"], detail="totp only")
    resp = ok(username=admin["username"], csrf=csrf, expires_in=config.ADMIN_SESSION_TTL)
    _cookie(resp, request, sid, config.ADMIN_SESSION_TTL)
    return resp


@router.post("/api/login/totp")
async def login_totp(request: Request):
    ip = security.client_ip(request)
    s = security.get_admin_session(request)
    if not s or s.get("stage") not in ("totp_pending", "active"):
        return err("登录会话已过期，请重新输入账号密码", 401)
    if s.get("stage") == "active":
        return ok(username=s["username"], csrf=s["csrf"])
    allowed, retry = security.hit("totp:%s" % ip, 8, 600)
    if not allowed:
        return err("尝试过于频繁，请 %d 秒后再试" % retry, 429, retry_after=retry)
    body = await _body(request)
    code = str(body.get("code") or "").strip()[:8]
    admin = _admin_row(s["admin_id"])
    ukey = "u:" + str(admin["username"]).lower()
    for gk in (ukey, "ip:" + ip):
        okg, wait = security.guard_check(gk)
        if not okg:
            db.conn().execute("DELETE FROM admin_sessions WHERE sid=?", (s["sid"],))
            security.audit("admin_totp_locked", "alert", ip=ip, actor=admin["username"],
                           detail={"wait": wait})
            return err("尝试过多，请 %d 分钟后再试" % max(1, wait // 60 + 1), 429, retry_after=wait)
    secret = security.dec(admin["totp_secret"])
    if not security.verify_totp(secret, code):
        banned = security.record_failure(ip, "admin_totp_fail", threshold=6, ban_seconds=3600)
        lock_u = security.guard_fail(ukey, threshold=5, window=900, base_lock=600)
        lock_ip = security.guard_fail("ip:" + ip, threshold=10, window=900, base_lock=900)
        if lock_u or lock_ip:
            # Drop the half-finished login too, so the attacker must redo step 1.
            db.conn().execute("DELETE FROM admin_sessions WHERE sid=?", (s["sid"],))
        await asyncio.sleep(0.25 + random.random() * 0.35)
        lock = max(lock_u, lock_ip)
        msg = "动态验证码错误"
        if lock:
            msg += "，已锁定 %d 分钟" % max(1, lock // 60)
        elif banned:
            msg += "，IP 已临时封禁"
        return err(msg, 401, retry_after=lock or None)
    security.guard_ok(ukey)
    security.guard_ok("ip:" + ip)
    db.conn().execute("UPDATE admin_sessions SET stage='active', expires_at=? WHERE sid=?",
                      (int(time.time()) + config.ADMIN_SESSION_TTL, s["sid"]))
    db.conn().execute("UPDATE admins SET last_login_at=?, last_login_ip=? WHERE id=?",
                      (int(time.time()), ip, admin["id"]))
    security.audit("admin_login", "alert", ip=ip, actor=admin["username"], detail="totp ok")
    return ok(username=admin["username"], csrf=s["csrf"], expires_in=config.ADMIN_SESSION_TTL)


@router.post("/api/logout")
def logout(request: Request):
    s = security.get_admin_session(request)
    if s:
        db.conn().execute("DELETE FROM admin_sessions WHERE sid=?", (s["sid"],))
        security.audit("admin_logout", "info", ip=security.client_ip(request), actor=s["username"])
    resp = ok()
    resp.delete_cookie(config.ADMIN_COOKIE, path="/")
    return resp


@router.get("/api/me")
def me(request: Request):
    s = _admin(request)
    admin = _admin_row(s["admin_id"])
    return {"ok": True, "username": s["username"], "csrf": s["csrf"],
            "last_login_at": admin["last_login_at"], "last_login_ip": admin["last_login_ip"],
            "totp_on": bool(admin["totp_on"]), "site_name": db.get_setting("site_name", config.SITE_NAME)}


# ------------------------------------------------------------------ dashboard
@router.get("/api/overview")
def overview(request: Request):
    _admin(request)
    now = int(time.time())
    day = 86400
    def one(sql, args=()):
        r = db.q1(sql, args)
        return int(list(r)[0]) if r else 0
    cards_total = one("SELECT COUNT(*) FROM cards")
    cards_used = one("SELECT COUNT(*) FROM cards WHERE status='used'")
    cards_active = one("SELECT COUNT(*) FROM cards WHERE status='active'")
    cards_disabled = one("SELECT COUNT(*) FROM cards WHERE status='disabled'")
    cards_today = one("SELECT COUNT(*) FROM cards WHERE created_at>?", (now - day,))
    codes_total = one("SELECT COUNT(*) FROM sms_records WHERE status='received'")
    codes_today = one("SELECT COUNT(*) FROM sms_records WHERE status='received' AND got_at>?", (now - day,))
    redemptions_today = one("SELECT COUNT(*) FROM redemptions WHERE created_at>?", (now - day,))
    blocked = one("SELECT COUNT(*) FROM ips WHERE blocked_until>?", (now,))
    ips_total = one("SELECT COUNT(*) FROM ips")
    req_today = one("SELECT COUNT(*) FROM events WHERE ts>?", (now - day,))
    stock = {c: providers.stock(c) for c in providers.enabled_countries()}
    queue = nexq.stats()
    alloc_today = one("SELECT COUNT(*) FROM numbers WHERE allocated_at>?", (now - day,))

    series = []
    for i in range(13, -1, -1):
        start = now - i * day
        d0 = start - ((start - 8 * 3600) % day)
        d1 = d0 + day
        series.append({
            "day": time.strftime("%m-%d", time.localtime(d0)),
            "codes": one("SELECT COUNT(*) FROM sms_records WHERE status='received' AND got_at>=? AND got_at<?", (d0, d1)),
            "redemptions": one("SELECT COUNT(*) FROM redemptions WHERE result='ok' AND created_at>=? AND created_at<?", (d0, d1)),
        })
    recent = [dict(r) for r in db.q(
        "SELECT ts,kind,level,ip,actor,card_id,detail FROM events ORDER BY id DESC LIMIT 12")]
    return {"ok": True, "stats": {
        "cards_total": cards_total, "cards_used": cards_used, "cards_active": cards_active,
        "cards_disabled": cards_disabled, "cards_today": cards_today,
        "codes_total": codes_total, "codes_today": codes_today,
        "redemptions_today": redemptions_today, "blocked_ips": blocked, "ips_total": ips_total,
        "events_today": req_today, "alloc_today": alloc_today, "stock": stock,
        "queue": queue, "cooldown": providers.cooldown_left(),
    }, "series": series, "recent": recent,
        "provider": db.get_setting("provider", "pool")}


# ------------------------------------------------------------------ cards
def gen_key() -> tuple[str, str]:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(16))
    key = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
    return key, hashlib.sha256(("sg:" + key).encode()).hexdigest()


@router.post("/api/cards/create")
async def cards_create(request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    try:
        count = int(body.get("count") or 1)
    except Exception:
        count = 1
    count = max(1, min(count, 500))
    note = str(body.get("note") or "")[:120]
    try:
        max_codes = max(1, min(int(body.get("max_codes") or 1), 20))
    except Exception:
        max_codes = 1
    try:
        days = int(body.get("expires_days") or 0)
    except Exception:
        days = 0
    # Optional pre-bound segment: the generated link can only ever use this one
    # choice, and the public page will not even show the other segments.
    choice = str(body.get("choice") or "").strip().lower()
    if choice and choice not in providers.CHOICES:
        return err("无效的号段")
    now = int(time.time())
    batch_no = "B" + time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2).upper()
    keys = []
    with db.tx() as c:
        cur = c.execute("INSERT INTO batches(batch_no,count,note,created_by,created_at) VALUES(?,?,?,?,?)",
                        (batch_no, count, note, s["username"], now))
        bid = cur.lastrowid
        for _ in range(count):
            for _try in range(6):
                key, kh = gen_key()
                try:
                    c.execute(
                        "INSERT INTO cards(key_hash,key_prefix,note,max_codes,status,batch_id,created_at,created_by,expires_at,choice) "
                        "VALUES(?,?,?,?,'active',?,?,?,?,?)",
                        (kh, key[:9], note, max_codes, bid, now, s["username"],
                         now + days * 86400 if days else 0, choice))
                    keys.append(key)
                    break
                except Exception:
                    continue
    security.audit("cards_create", "alert", ip=security.client_ip(request), actor=s["username"],
                   detail={"batch": batch_no, "count": len(keys), "max_codes": max_codes,
                           "choice": choice})
    base = (db.get_setting("site_url", "") or "").rstrip("/")
    return ok(batch=batch_no, keys=keys, links=[base + "/c/" + k for k in keys] if base else [],
              count=len(keys), choice=choice)


@router.get("/api/cards")
def cards_list(request: Request, status: str = "", q: str = "", page: int = 1, size: int = 50):
    _admin(request)
    where, args = [], []
    now = int(time.time())
    if status and status != "all":
        if status == "active":
            where.append("c.status='active' AND c.used_codes<c.max_codes AND (c.expires_at=0 OR c.expires_at>?)")
            args.append(now)
        elif status == "used":
            where.append("(c.status='used' OR c.used_codes>=c.max_codes)")
        elif status == "disabled":
            where.append("c.status='disabled'")
        elif status == "expired":
            where.append("(c.expires_at>0 AND c.expires_at<?)")
            args.append(now)
    if q:
        where.append("(c.key_prefix LIKE ? OR c.note LIKE ? OR b.batch_no LIKE ?)")
        args += ["%" + q + "%"] * 3
    w = (" WHERE " + " AND ".join(where)) if where else ""
    page = max(1, page)
    size = max(1, min(size, 200))
    total = db.q1("SELECT COUNT(*) AS n FROM cards c LEFT JOIN batches b ON b.id=c.batch_id" + w, tuple(args))["n"]
    rows = db.q(
        "SELECT c.id,c.key_prefix,c.note,c.max_codes,c.used_codes,c.status,c.created_at,"
        "c.first_used_at,c.last_used_at,c.last_ip,c.expires_at,c.batch_id,b.batch_no,"
        "c.choice AS bound_choice,cc.choice AS choice "
        "FROM cards c LEFT JOIN batches b ON b.id=c.batch_id "
        "LEFT JOIN card_choices cc ON cc.card_id=c.id" + w +
        " ORDER BY c.id DESC LIMIT ? OFFSET ?", tuple(args) + (size, (page - 1) * size))
    return {"ok": True, "total": total, "page": page, "items": [dict(r) for r in rows], "now": now}


@router.post("/api/cards/{card_id}/action")
async def card_action(card_id: int, request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    act = str(body.get("action") or "")
    card = db.q1("SELECT * FROM cards WHERE id=?", (card_id,))
    if not card:
        return err("卡密不存在", 404)
    if act == "disable":
        db.conn().execute("UPDATE cards SET status='disabled' WHERE id=?", (card_id,))
    elif act == "enable":
        if card["used_codes"] >= card["max_codes"]:
            db.conn().execute("UPDATE cards SET status='used' WHERE id=?", (card_id,))
        else:
            db.conn().execute("UPDATE cards SET status='active' WHERE id=?", (card_id,))
    elif act == "reset_choice":
        # Operator override: unlock the one-shot choice and release the number so the
        # card holder can start over (e.g. the upstream handed out a dead number).
        with db.tx() as c:
            c.execute("DELETE FROM card_choices WHERE card_id=?", (card_id,))
            c.execute("UPDATE numbers SET card_id=0, status='expired' "
                      "WHERE card_id=? AND status='allocated'", (card_id,))
            c.execute("UPDATE sms_records SET status='expired' WHERE card_id=? AND status='waiting'",
                      (card_id,))
            c.execute("DELETE FROM card_sessions WHERE card_id=?", (card_id,))
    elif act == "delete":
        db.conn().execute("DELETE FROM card_sessions WHERE card_id=?", (card_id,))
        db.conn().execute("DELETE FROM card_choices WHERE card_id=?", (card_id,))
        db.conn().execute("DELETE FROM cards WHERE id=?", (card_id,))
    else:
        return err("未知操作")
    security.audit("card_" + act, "alert", ip=security.client_ip(request), actor=s["username"],
                   card_id=card_id, detail={"prefix": card["key_prefix"]})
    return ok()


# ------------------------------------------------------------------ numbers
@router.post("/api/numbers/import")
async def numbers_import(request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    country = str(body.get("country") or "us").lower()
    if country not in providers.COUNTRIES:
        return err("不支持的地区")
    text = str(body.get("lines") or "")
    note = str(body.get("note") or "")[:80]
    added, skipped = 0, 0
    now = int(time.time())
    with db.tx() as c:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            phone, url = "", ""
            for sep in ("----", "|", ",", "\t"):
                if sep in line:
                    phone, url = line.split(sep, 1)
                    break
            if not url:
                parts = line.split()
                if len(parts) >= 2:
                    phone, url = parts[0], " ".join(parts[1:])
            phone = phone.strip()
            url = url.strip()
            if phone.startswith("+44"):
                row_country = "gb"
            elif phone.startswith("+852"):
                row_country = "hk"
            elif phone.startswith("+1"):
                row_country = "us"
            else:
                row_country = country
            if not phone or not url:
                skipped += 1
                continue
            exists = c.execute("SELECT id FROM numbers WHERE phone=? AND sms_url=?", (phone, url)).fetchone()
            if exists:
                skipped += 1
                continue
            c.execute(
                "INSERT INTO numbers(phone,country,provider,sms_url,status,note,created_at) "
                "VALUES(?,?,?,?,'available',?,?)",
                (phone, row_country, "pool", url, note, now))
            added += 1
    security.audit("numbers_import", "alert", ip=security.client_ip(request), actor=s["username"],
                   detail={"country": country, "added": added, "skipped": skipped})
    return ok(added=added, skipped=skipped)


@router.get("/api/numbers")
def numbers_list(request: Request, status: str = "", country: str = "", q: str = "",
                 page: int = 1, size: int = 50):
    _admin(request)
    where, args = [], []
    if status and status != "all":
        where.append("n.status=?")
        args.append(status)
    if country and country != "all":
        where.append("n.country=?")
        args.append(country)
    if q:
        where.append("(n.phone LIKE ? OR n.note LIKE ?)")
        args += ["%" + q + "%"] * 2
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.q1("SELECT COUNT(*) AS n FROM numbers n" + w, tuple(args))["n"]
    rows = db.q("SELECT n.*, (SELECT COUNT(*) FROM sms_records s WHERE s.number_id=n.id AND s.status='received') AS codes "
                "FROM numbers n" + w + " ORDER BY n.id DESC LIMIT ? OFFSET ?",
                tuple(args) + (max(1, min(size, 200)), (max(1, page) - 1) * max(1, min(size, 200))))
    return {"ok": True, "total": total, "page": page, "items": [dict(r) for r in rows]}


@router.post("/api/numbers/{num_id}/action")
async def number_action(num_id: int, request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    act = str(body.get("action") or "")
    row = db.q1("SELECT * FROM numbers WHERE id=?", (num_id,))
    if not row:
        return err("号码不存在", 404)
    if act == "disable":
        db.conn().execute("UPDATE numbers SET status='disabled' WHERE id=?", (num_id,))
    elif act == "enable":
        db.conn().execute("UPDATE numbers SET status='available', card_id=0, allocated_at=0 WHERE id=?", (num_id,))
    elif act == "delete":
        db.conn().execute("DELETE FROM numbers WHERE id=?", (num_id,))
    else:
        return err("未知操作")
    security.audit("number_" + act, "alert", ip=security.client_ip(request), actor=s["username"],
                   detail={"phone": row["phone"]})
    return ok()


# ------------------------------------------------------------------ records
@router.get("/api/usage")
def usage(request: Request, q: str = "", page: int = 1, size: int = 50):
    _admin(request)
    where, args = [], []
    if q:
        where.append("(s.ip LIKE ? OR c.key_prefix LIKE ?)")
        args += ["%" + q + "%"] * 2
    w = (" WHERE " + " AND ".join(where)) if where else ""
    page = max(1, page)
    size = max(1, min(size, 200))
    total = db.q1("SELECT COUNT(*) AS n FROM card_sessions s JOIN cards c ON c.id=s.card_id" + w, tuple(args))["n"]
    rows = db.q(
        "SELECT s.sid,s.ip,s.ua,s.created_at,s.last_seen,s.expires_at,c.id AS card_id,c.key_prefix,"
        "c.used_codes,c.max_codes,"
        "(SELECT phone FROM numbers n WHERE n.card_id=c.id ORDER BY n.id DESC LIMIT 1) AS phone "
        "FROM card_sessions s JOIN cards c ON c.id=s.card_id" + w +
        " ORDER BY s.last_seen DESC LIMIT ? OFFSET ?", tuple(args) + (size, (page - 1) * size))
    return {"ok": True, "total": total, "page": page, "items": [dict(r) for r in rows],
            "now": int(time.time())}


@router.get("/api/redemptions")
def redemptions(request: Request, result: str = "", q: str = "", page: int = 1, size: int = 50):
    _admin(request)
    where, args = [], []
    if result and result != "all":
        where.append("r.result=?")
        args.append(result)
    if q:
        where.append("(r.ip LIKE ? OR c.key_prefix LIKE ?)")
        args += ["%" + q + "%"] * 2
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.q1("SELECT COUNT(*) AS n FROM redemptions r LEFT JOIN cards c ON c.id=r.card_id" + w, tuple(args))["n"]
    rows = db.q("SELECT r.*, c.key_prefix FROM redemptions r LEFT JOIN cards c ON c.id=r.card_id" + w +
                " ORDER BY r.id DESC LIMIT ? OFFSET ?",
                tuple(args) + (max(1, min(size, 200)), (max(1, page) - 1) * max(1, min(size, 200))))
    return {"ok": True, "total": total, "page": page, "items": [dict(r) for r in rows]}


@router.get("/api/codes")
def codes(request: Request, status: str = "", q: str = "", page: int = 1, size: int = 50):
    _admin(request)
    where, args = [], []
    if status and status != "all":
        where.append("s.status=?")
        args.append(status)
    if q:
        where.append("(s.phone LIKE ? OR s.code LIKE ? OR c.key_prefix LIKE ?)")
        args += ["%" + q + "%"] * 3
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.q1("SELECT COUNT(*) AS n FROM sms_records s LEFT JOIN cards c ON c.id=s.card_id" + w, tuple(args))["n"]
    rows = db.q("SELECT s.*, c.key_prefix, n.provider FROM sms_records s "
                "LEFT JOIN cards c ON c.id=s.card_id LEFT JOIN numbers n ON n.id=s.number_id" + w +
                " ORDER BY s.id DESC LIMIT ? OFFSET ?",
                tuple(args) + (max(1, min(size, 200)), (max(1, page) - 1) * max(1, min(size, 200))))
    items = []
    for r in rows:
        d = dict(r)
        d["provider"] = d.get("provider") or "pool"
        items.append(d)
    return {"ok": True, "total": total, "page": page, "items": items}


@router.get("/api/audit")
def audit_list(request: Request, level: str = "", kind: str = "", q: str = "",
               page: int = 1, size: int = 60):
    _admin(request)
    where, args = [], []
    if level and level != "all":
        where.append("level=?")
        args.append(level)
    if kind:
        where.append("kind LIKE ?")
        args.append("%" + kind + "%")
    if q:
        where.append("(ip LIKE ? OR actor LIKE ? OR detail LIKE ?)")
        args += ["%" + q + "%"] * 3
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.q1("SELECT COUNT(*) AS n FROM events" + w, tuple(args))["n"]
    rows = db.q("SELECT * FROM events" + w + " ORDER BY id DESC LIMIT ? OFFSET ?",
                tuple(args) + (max(1, min(size, 300)), (max(1, page) - 1) * max(1, min(size, 300))))
    return {"ok": True, "total": total, "page": page, "items": [dict(r) for r in rows]}


# ------------------------------------------------------------------ IPs
@router.get("/api/ips")
def ip_list(request: Request, state: str = "", q: str = "", page: int = 1, size: int = 60):
    _admin(request)
    now = int(time.time())
    where, args = [], []
    if state == "blocked":
        where.append("blocked_until>?")
        args.append(now)
    elif state == "clean":
        where.append("blocked_until<=?")
        args.append(now)
    if q:
        where.append("ip LIKE ?")
        args.append("%" + q + "%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.q1("SELECT COUNT(*) AS n FROM ips" + w, tuple(args))["n"]
    rows = db.q("SELECT * FROM ips" + w + " ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                tuple(args) + (max(1, min(size, 300)), (max(1, page) - 1) * max(1, min(size, 300))))
    items = []
    for r in rows:
        d = dict(r)
        d["blocked"] = d["blocked_until"] > now
        d["blocked_for"] = max(0, d["blocked_until"] - now)
        items.append(d)
    return {"ok": True, "total": total, "page": page, "now": now, "items": items}


@router.post("/api/ips/action")
async def ip_action(request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    ip = str(body.get("ip") or "").strip()
    act = str(body.get("action") or "")
    if not ip or len(ip) > 64:
        return err("IP 不合法")
    if act == "block":
        try:
            minutes = max(1, min(int(body.get("minutes") or 60), 60 * 24 * 365))
        except Exception:
            minutes = 60
        reason = str(body.get("reason") or "manual")[:120]
        security.ban_ip(ip, minutes * 60, "manual:" + reason)
    elif act == "unblock":
        security.unban_ip(ip)
    elif act == "note":
        note = str(body.get("note") or "")[:200]
        db.conn().execute("UPDATE ips SET note=? WHERE ip=?", (note, ip))
    else:
        return err("未知操作")
    security.audit("ip_" + act, "alert", ip=security.client_ip(request), actor=s["username"],
                   detail={"target": ip, "body": {k: v for k, v in body.items() if k != "csrf"}})
    return ok()


# ------------------------------------------------------------------ settings
SECRET_KEYS = {"sms_activate_key", "fivesim_key", "nexsms_key", "nexsms_proxy", "d1jiema_token"}
ALLOWED = {
    "site_name", "site_url", "site_note", "provider", "poll_interval", "number_ttl_min",
    "max_numbers_per_card", "max_alloc_attempts", "sms_activate_key", "sms_activate_service", "fivesim_key",
    "fivesim_product", "fivesim_operator", "allow_countries", "auto_ban_enabled",
    "nexsms_key", "nexsms_base", "nexsms_project", "nexsms_sms_prefix", "nexsms_type",
    "nexsms_expiry", "nexsms_prefix", "nexsms_app_id", "nexsms_batch_qty",
    "nexsms_stock_target", "nexsms_pool_ttl_min", "nexsms_buy_wait_sec",
    "nexsms_queue_concurrency", "nexsms_queue_max", "nexsms_queue_timeout_sec",
    "nexsms_cooldown_sec", "nexsms_proxy", "admin_ip_allow", "exit_grace_sec",
    "shop_url", "allow_choices", "d1jiema_enabled", "d1jiema_token", "d1jiema_base",
    "d1jiema_keyword", "d1jiema_sms_prefix", "d1jiema_gap_ms",
    "relay_base", "relay_rate", "relay_ip_rate", "relay_bad_rate", "relay_on",
}


@router.get("/api/settings")
def get_settings(request: Request):
    _admin(request)
    data = db.all_settings()
    out = {}
    for k, v in data.items():
        if k in SECRET_KEYS:
            out[k] = ""
            out[k + "_set"] = bool(v)
            out[k + "_hint"] = ("****" + v[-4:]) if v else ""
        else:
            out[k] = v
    return {"ok": True, "settings": out}


@router.post("/api/settings")
async def save_settings(request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    changed = []
    for k, v in body.items():
        if k not in ALLOWED:
            continue
        v = str(v if v is not None else "").strip()
        if k in SECRET_KEYS and not v:
            continue
        db.set_setting(k, v[:400])
        if k in SECRET_KEYS:
            changed.append(k + "(set)")
        else:
            changed.append(k + "=" + v[:60])
    security.audit("settings_save", "alert", ip=security.client_ip(request), actor=s["username"],
                   detail={"changed": changed})
    return ok(changed=changed)


@router.get("/api/queue/stats")
def queue_stats(request: Request):
    _admin(request)
    return ok(stats=nexq.stats(), cooldown=providers.cooldown_left(),
              streak=db.get_setting("nexsms_429_streak", "0"),
              supply_block=providers.supply_block_left(),
              supply_msg=providers.supply_block_message())


@router.post("/api/queue/cooldown")
async def queue_cooldown(request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    if str(body.get("action") or "clear") == "clear":
        providers.cooldown_clear()
        security.audit("nexsms_cooldown_clear", "alert", ip=security.client_ip(request),
                       actor=s["username"], detail={})
        return ok(cooldown=0)
    return err("未知操作")


@router.post("/api/nexsms/refill")
async def nexsms_refill(request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    if db.get_setting("provider", "pool") != "nexsms_premium":
        return err("当前号源不是 NexSMS 优选")
    out = {}
    for c in providers.enabled_countries():
        try:
            out[c] = providers.refill_now(c)
        except Exception as e:
            out[c] = {"error": str(e)[:160]}
    security.audit("nexsms_refill", "alert", ip=security.client_ip(request), actor=s["username"],
                   detail=out)
    return ok(result=out)


@router.post("/api/provider/test")
async def provider_test(request: Request):
    s = _admin(request)
    body = await _body(request)
    _csrf(request, s, body)
    res = providers.test_provider(str(body.get("provider") or ""))
    security.audit("provider_test", "info", ip=security.client_ip(request), actor=s["username"],
                   detail=res)
    return ok(**res)