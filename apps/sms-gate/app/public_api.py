# -*- coding: utf-8 -*-
"""Public (card holder) API."""
from __future__ import annotations
import hashlib, json, re, time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import config, db, nexq, providers, security

router = APIRouter()

KEY_RE = re.compile(r"^[A-Z0-9-]{8,32}$")


def ok(**kw):
    kw.setdefault("ok", True)
    return JSONResponse(kw)


def err(msg: str, code: int = 400, **kw):
    kw.update({"ok": False, "error": msg})
    return JSONResponse(kw, status_code=code)


def norm_key(raw: str) -> str:
    s = (raw or "").strip().upper().replace(" ", "")
    s = s.removeprefix("HTTPS://").removeprefix("HTTP://")
    if "/C/" in s:
        s = s.split("/C/", 1)[1]
    s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return s


def key_hash(key: str) -> str:
    return hashlib.sha256(("sg:" + key).encode()).hexdigest()


def card_state(card) -> str:
    now = int(time.time())
    if card["status"] == "disabled":
        return "disabled"
    if card["status"] == "used" or card["used_codes"] >= card["max_codes"]:
        return "used"
    if card["expires_at"] and card["expires_at"] < now:
        return "expired"
    return "active"


def _set_cookie(resp: JSONResponse, request: Request, sid: str) -> None:
    resp.set_cookie(config.CARD_COOKIE, sid, max_age=config.CARD_SESSION_TTL,
                    httponly=True, samesite="lax", secure=security.is_https(request), path="/")


def _session_payload(sess: dict) -> dict:
    card = db.q1("SELECT * FROM cards WHERE id=?", (sess["card_id"],))
    if not card:
        return {"ok": False, "error": "卡密不存在"}
    state = card_state(card)
    num = db.q1("SELECT * FROM numbers WHERE card_id=? ORDER BY id DESC LIMIT 1", (card["id"],))
    sms = db.q1("SELECT * FROM sms_records WHERE card_id=? ORDER BY id DESC LIMIT 1", (card["id"],))
    countries = []
    for c in providers.enabled_countries():
        countries.append({
            "code": c,
            "name": providers.COUNTRIES[c]["name"],
            "flag": providers.COUNTRIES[c]["flag"],
            "stock": providers.stock(c),
        })
    bound = _bound_choice(card)
    locked = _locked_choice(card["id"])
    # Hide every other segment as soon as the card is pre-bound or already claimed:
    # the holder must not even see options they cannot use.
    only = bound or (str(locked["choice"]) if locked else "")
    offered = [only] if only in providers.CHOICES else _choice_offered(card)
    choices = []
    for c in offered:
        info = providers.CHOICES.get(c) or {}
        choices.append({
            "code": c, "name": info.get("name", c), "flag": info.get("flag", ""),
            "sub": info.get("sub", ""), "stock": providers.stock_for_choice(c),
        })
    num_out = None
    if num:
        ttl_left = 0
        if num["status"] == "allocated" and num["allocated_at"]:
            ttl_left = max(0, int(num["allocated_at"] + config.NUMBER_TTL_MIN * 60 - time.time()))
        num_out = _num_view(num, ttl_left)
    sms_out = None
    if sms:
        sms_out = {"state": sms["status"], "code": sms["code"] if sms["status"] == "received" else "",
                   "polls": sms["polls"], "at": sms["got_at"]}
    return {
        "ok": True,
        "csrf": sess["csrf"],
        "card": {"prefix": card["key_prefix"], "status": state,
                 "remaining": max(0, card["max_codes"] - card["used_codes"]),
                 "expires_at": card["expires_at"]},
        "number": num_out,
        "sms": sms_out,
        "queue": None,
        "countries": countries,
        "choices": choices,
        "bound": bound,
        "choice": (_choice_view(locked["choice"]) if locked else
                   (_choice_view(bound) if bound else None)),
        "poll_interval": int(db.get_setting("poll_interval", str(config.POLL_INTERVAL)) or config.POLL_INTERVAL),
        "cooldown": (providers.cooldown_left()
                     if db.get_setting("provider", "pool") == "nexsms_premium" else 0),
        "note": db.get_setting("site_note", ""),
        "server_time": int(time.time()),
    }


# ------------------------------------------------------------------ meta / health
@router.get("/api/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@router.get("/api/meta")
def meta():
    return {
        "ok": True,
        "site_name": db.get_setting("site_name", config.SITE_NAME),
        "note": db.get_setting("site_note", ""),
        "poll_interval": int(db.get_setting("poll_interval", "15") or 15),
    }


# ------------------------------------------------------------------ redeem
@router.post("/api/redeem")
async def redeem(request: Request):
    ip = security.client_ip(request)
    # Shared/NAT IPs must not block each other: cap per IP loosely, then per card key.
    allowed, retry = security.hit("redeem:%s" % ip, 300, 600)
    if not allowed:
        return err("操作过于频繁，请 %d 秒后再试" % retry, 429, retry_after=retry)

    try:
        body = await request.json()
    except Exception:
        body = {}
    key = norm_key(str(body.get("key") or ""))
    if not key or not KEY_RE.match(key):
        security.record_failure(ip, "redeem_invalid", threshold=8, ban_seconds=3600)
        return err("卡密格式不正确", 400)
    allowed, retry = security.hit("redeemk:%s" % key, 8, 600)
    if not allowed:
        return err("该卡密尝试过于频繁，请 %d 秒后再试" % retry, 429, retry_after=retry)

    card = db.q1("SELECT * FROM cards WHERE key_hash=?", (key_hash(key),))
    if not card:
        banned = security.record_failure(ip, "redeem_fail", threshold=8, ban_seconds=3600)
        db.conn().execute(
            "INSERT INTO redemptions(card_id,ip,ua,result,detail,created_at) VALUES(0,?,?,?,?,?)",
            (ip, security.ua(request), "fail", "invalid key", int(time.time())))
        return err("卡密无效或已被销毁" + ("，IP 已临时封禁" if banned else ""), 404)

    state = card_state(card)
    if state == "disabled":
        db.conn().execute("INSERT INTO redemptions(card_id,ip,ua,result,detail,created_at) VALUES(?,?,?,?,?,?)",
                          (card["id"], ip, security.ua(request), "fail", "disabled", int(time.time())))
        return err("该卡密已被禁用", 403)
    if state == "used":
        return err("该卡密已完成接码，无法重复使用", 409)
    if state == "expired":
        return err("该卡密已过期", 410)

    sid, csrf = security.create_card_session(card["id"], ip, security.ua(request))
    db.conn().execute(
        "INSERT INTO redemptions(card_id,ip,ua,result,detail,created_at) VALUES(?,?,?,?,?,?)",
        (card["id"], ip, security.ua(request), "ok", "session created", int(time.time())))
    if not card["first_used_at"]:
        db.conn().execute("UPDATE cards SET first_used_at=?, last_ip=? WHERE id=?",
                          (int(time.time()), ip, card["id"]))
    security.audit("card_redeem", "info", ip=ip, card_id=card["id"], detail={"prefix": card["key_prefix"]})
    resp = ok(csrf=csrf, card={"prefix": card["key_prefix"], "status": state,
                               "remaining": max(0, card["max_codes"] - card["used_codes"])})
    _set_cookie(resp, request, sid)
    return resp


@router.get("/api/session")
def session(request: Request):
    sess = security.get_card_session(request)
    if not sess:
        return err("未授权", 401)
    payload = _session_payload(sess)
    if not payload.get("ok"):
        return err(payload.get("error", "会话异常"), 401)
    return JSONResponse(payload)


# ------------------------------------------------------------------ choice lock
NUMBER_KEYS = {"csrf", "choice", "country", "retry"}


def _choice_view(code: str) -> dict:
    info = providers.CHOICES.get(code) or {}
    return {"code": code, "name": info.get("name", code), "flag": info.get("flag", ""),
            "sub": info.get("sub", ""), "provider": providers.provider_for_choice(code)}


def _bound_choice(card) -> str:
    """Segment pre-bound by the operator when the card was generated ('' = free)."""
    try:
        code = str(card["choice"] or "").strip().lower()
    except Exception:
        code = ""
    return code if code in providers.CHOICES else ""


def _choice_offered(card) -> list:
    """Only the bound segment is exposed when the card is pre-bound."""
    bound = _bound_choice(card)
    if bound:
        return [bound]
    return list(providers.enabled_choices())


def _locked_choice(card_id: int):
    r = db.q1("SELECT * FROM card_choices WHERE card_id=?", (card_id,))
    return dict(r) if r else None


def _claim_choice(card_id: int, choice: str, ip: str):
    """Atomically bind a card to one choice (INSERT .. ON CONFLICT DO NOTHING)."""
    now = int(time.time())
    info = providers.CHOICES.get(choice) or {}
    prov = providers.provider_for_choice(choice)
    card_type = str(info.get("card_type") or "")
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO card_choices(card_id,choice,provider,card_type,ip,created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(card_id) DO NOTHING",
            (card_id, choice, prov, card_type, ip, now))
        created = cur.rowcount == 1
        row = c.execute("SELECT * FROM card_choices WHERE card_id=?", (card_id,)).fetchone()
    return dict(row), created


def _choice_mismatch(ip, card_id, locked: dict, want: str, where: str):
    security.audit("choice_violation", "alert", ip=ip, card_id=card_id,
                   detail={"where": where, "locked": locked.get("choice"), "want": want})
    name = (providers.CHOICES.get(locked.get("choice")) or {}).get("name", locked.get("choice"))
    return err("本卡密已锁定「%s」号段，无法更改" % name, 409, locked=locked.get("choice"))


def _num_view(num, ttl_left: int = 0) -> dict:
    d = dict(num)
    ch = providers.number_choice(d)
    info = providers.CHOICES.get(ch) or {}
    country = d.get("country") or ""
    return {
        "phone": d.get("phone"), "country": country, "choice": ch,
        "status": d.get("status", ""), "ttl_left": max(0, int(ttl_left or 0)),
        "flag": info.get("flag") or providers.COUNTRIES.get(country, {}).get("flag", ""),
        "name": info.get("name") or providers.COUNTRIES.get(country, {}).get("name", country),
    }


@router.post("/api/choice")
async def choose(request: Request):
    """Commit the one and only choice for this card. Irreversible and idempotent."""
    ip = security.client_ip(request)
    sess = security.get_card_session(request)
    if not sess:
        return err("请先验证卡密", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    security.require_csrf(request, request.headers.get("x-csrf", "") or body.get("csrf", ""), sess["csrf"])
    unknown = set(body) - {"csrf", "choice"}
    if unknown:
        security.audit("request_tamper", "alert", ip=ip, card_id=sess["card_id"],
                       detail={"endpoint": "choice", "keys": sorted(unknown)[:8]})
        return err("请求包含非法参数", 400)
    choice = str(body.get("choice") or "").strip().lower()
    if choice not in providers.CHOICES:
        return err("无效的号段", 400)
    card = db.q1("SELECT * FROM cards WHERE id=?", (sess["card_id"],))
    if not card or card_state(card) != "active":
        return err("该卡密已使用或已失效", 403)
    bound = _bound_choice(card)
    if bound and choice != bound:
        return _choice_mismatch(ip, card["id"], {"choice": bound}, choice, "choose_bound")
    if choice not in providers.enabled_choices() and choice != bound:
        return err("该号段暂未开放", 403)
    row, created = _claim_choice(card["id"], choice, ip)
    if not created and row["choice"] != choice:
        return _choice_mismatch(ip, card["id"], row, choice, "choose")
    if created:
        security.audit("choice_lock", "info", ip=ip, card_id=card["id"],
                       detail={"choice": choice, "provider": row["provider"]})
    return ok(choice=_choice_view(row["choice"]), locked=True)


# ------------------------------------------------------------------ number
@router.post("/api/number")
async def take_number(request: Request):
    ip = security.client_ip(request)
    sess = security.get_card_session(request)
    if not sess:
        return err("请先验证卡密", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    security.require_csrf(request, request.headers.get("x-csrf", "") or body.get("csrf", ""), sess["csrf"])

    # Tamper guard: the page only ever sends {csrf, choice}. Anything else (quantity,
    # provider, cardType, count, ...) is an attempt to steer the purchase server-side.
    unknown = set(body) - NUMBER_KEYS
    if unknown:
        security.audit("request_tamper", "alert", ip=ip, card_id=sess["card_id"],
                       detail={"endpoint": "number", "keys": sorted(unknown)[:8]})
        return err("请求包含非法参数", 400)

    allowed, retry = security.hit("numip:%s" % ip, 300, 600)
    if not allowed:
        return err("请求过于频繁，请 %d 秒后再试" % retry, 429, retry_after=retry)
    allowed, retry = security.hit("num:%s" % sess["card_id"], 12, 600)
    if not allowed:
        return err("该卡密取号过于频繁，请 %d 秒后再试" % retry, 429, retry_after=retry)

    card = db.q1("SELECT * FROM cards WHERE id=?", (sess["card_id"],))
    if not card:
        return err("卡密不存在", 404)
    if card_state(card) != "active":
        return err("该卡密已使用或已失效", 403)

    raw = str(body.get("choice") or "").strip().lower()
    if not raw:
        # Backwards compatible: country=us/gb/hk maps 1:1 onto a choice.
        legacy = str(body.get("country") or "").strip().lower()
        raw = legacy if legacy in providers.CHOICES else ""
    if raw not in providers.CHOICES:
        return err("无效的号段", 400)
    bound = _bound_choice(card)
    if bound and raw != bound:
        return _choice_mismatch(ip, card["id"], {"choice": bound}, raw, "number_bound")
    if raw not in providers.enabled_choices() and raw != bound:
        return err("该号段暂未开放", 403)

    row, created = _claim_choice(card["id"], raw, ip)
    if not created and row["choice"] != raw:
        return _choice_mismatch(ip, card["id"], row, raw, "number")
    choice = row["choice"]
    if created:
        security.audit("choice_lock", "info", ip=ip, card_id=card["id"],
                       detail={"choice": choice, "provider": row["provider"]})

    info = providers.CHOICES.get(choice) or {}
    prov_for_choice = providers.provider_for_choice(choice)
    now = int(time.time())
    want_retry = bool(body.get("retry"))
    try:
        max_attempts = max(1, min(10, int(db.get_setting("max_alloc_attempts", "3") or 3)))
    except Exception:
        max_attempts = 3
    attempts = int(db.q1("SELECT COUNT(*) AS n FROM numbers WHERE card_id=?", (card["id"],))["n"])

    def _drop_number(n):
        # A number that never delivered a code must not burn the card: mark it expired,
        # free the one-shot slot so the card can try again, and release the upstream lease.
        db.conn().execute("UPDATE numbers SET status='expired' WHERE id=?", (n["id"],))
        db.conn().execute("UPDATE sms_records SET status='expired' WHERE number_id=? AND status='waiting'",
                          (n["id"],))
        db.conn().execute("UPDATE card_choices SET number_id=0 WHERE card_id=? AND number_id=?",
                          (card["id"], n["id"]))

    cur = db.q1("SELECT * FROM numbers WHERE card_id=? ORDER BY id DESC LIMIT 1", (card["id"],))
    if cur:
        if providers.number_choice(dict(cur)) != choice:
            return _choice_mismatch(ip, card["id"], row, choice, "number_reuse")
        if cur["status"] == "allocated":
            ttl = int(cur["allocated_at"] + config.NUMBER_TTL_MIN * 60 - now)
            if ttl > 0:
                held = max(0, now - int(cur["allocated_at"]))
                if want_retry:
                    if held < 45:
                        return ok(number=_num_view(cur, ttl), reused=True, retry_early=True,
                                  retry_in=45 - held, attempts=attempts, max_attempts=max_attempts)
                    if attempts >= max_attempts:
                        return err("本卡密换号次数已用完（%d 次），请等待当前号码或联系管理员" % max_attempts, 409)
                    _drop_number(cur)
                    await run_in_threadpool(providers.finish, dict(cur), False)
                    security.audit("number_retry", "warn", ip=ip, card_id=card["id"],
                                   detail={"phone": cur["phone"], "held": held, "attempts": attempts})
                    cur = None
                else:
                    nexq.cancel_live(card["id"])
                    return ok(number=_num_view(cur, ttl), reused=True,
                              attempts=attempts, max_attempts=max_attempts)
            else:
                _drop_number(cur)
        elif cur["status"] == "used":
            return err("该卡密已完成接码", 409)

    if attempts >= max_attempts:
        return err("本卡密换号次数已用完（%d 次），请联系管理员" % max_attempts, 409)

    # One allocation per card, enforced atomically. number_id: 0 = free, -1 = in flight,
    # >0 = a number already exists. A stale -1 self-heals after 90 seconds.
    now2 = int(time.time())
    slot = db.conn().execute(
        "UPDATE card_choices SET number_id=-1, alloc_at=? WHERE card_id=? AND "
        "(number_id=0 OR (number_id=-1 AND alloc_at<?))", (now2, card["id"], now2 - 90))
    if slot.rowcount != 1:
        return err("取号处理中，请稍候重试", 429, retry_after=2)

    def _reset_slot():
        try:
            db.conn().execute(
                "UPDATE card_choices SET number_id=0 WHERE card_id=? AND number_id=-1", (card["id"],))
        except Exception:
            pass

    try:
        if prov_for_choice == "d1jiema":
            num = await run_in_threadpool(providers.allocate_d1, choice,
                                          info.get("card_type", ""), card["id"])
        elif prov_for_choice == "nexsms_premium":
            num = await run_in_threadpool(providers.allocate_nex,
                                          info.get("country") or choice, card["id"])
        else:
            num = await run_in_threadpool(providers.allocate,
                                          info.get("country") or choice, card["id"])
    except providers.ProviderBusy as e:
        _reset_slot()
        blocked = providers.supply_block_left() > 0
        security.audit("allocate_busy", "warn", ip=ip, card_id=card["id"],
                       detail={"choice": choice, "retry": getattr(e, "retry_after", 0),
                               "blocked": blocked})
        return err(str(e), 429, retry_after=getattr(e, "retry_after", 3),
                   supply_blocked=blocked, supply_left=providers.supply_block_left())
    except providers.ProviderError as e:
        _reset_slot()
        return err(str(e), 409)
    except Exception as e:
        _reset_slot()
        security.audit("allocate_error", "error", ip=ip, card_id=card["id"],
                       detail=type(e).__name__)
        return err("取号失败，请稍后重试", 502)

    nexq.cancel_live(card["id"])
    db.conn().execute("UPDATE numbers SET choice=? WHERE id=?", (choice, num["id"]))
    db.conn().execute("UPDATE card_choices SET number_id=?, alloc_at=? WHERE card_id=?",
                      (num["id"], now2, card["id"]))
    db.conn().execute(
        "INSERT INTO sms_records(card_id,number_id,phone,country,status,created_at) VALUES(?,?,?,?,?,?)",
        (card["id"], num["id"], num["phone"], num["country"], "waiting", now))
    security.audit("number_alloc", "info", ip=ip, card_id=card["id"],
                   detail={"phone": num["phone"], "choice": choice})
    return ok(number=_num_view(num, config.NUMBER_TTL_MIN * 60),
              attempts=attempts + 1, max_attempts=max_attempts)


@router.post("/api/queue/status")
async def queue_status(request: Request):
    ip = security.client_ip(request)
    sess = security.get_card_session(request)
    if not sess:
        return err("请先验证卡密", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    security.require_csrf(request, request.headers.get("x-csrf", "") or body.get("csrf", ""), sess["csrf"])
    try:
        tid = int(body.get("ticket") or 0)
    except Exception:
        tid = 0
    info = nexq.status(tid, str(body.get("token") or ""), sess["card_id"])
    if not info:
        return err("排队信息已失效，请重新取号", 410)
    if info["state"] == "ready":
        num = db.q1("SELECT * FROM numbers WHERE id=?", (info["number_id"],))
        if not num:
            return err("号码已失效，请重新取号", 410)
        locked = _locked_choice(sess["card_id"])
        if locked and providers.number_choice(dict(num)) != locked["choice"]:
            return _choice_mismatch(ip, sess["card_id"], locked,
                                    providers.number_choice(dict(num)), "queue_ready")
        now = int(time.time())
        rec = db.q1("SELECT id FROM sms_records WHERE number_id=?", (num["id"],))
        if not rec:
            db.conn().execute(
                "INSERT INTO sms_records(card_id,number_id,phone,country,status,created_at) VALUES(?,?,?,?,?,?)",
                (sess["card_id"], num["id"], num["phone"], num["country"], "waiting", now))
        nexq.finish(tid)
        db.conn().execute("UPDATE card_choices SET number_id=? WHERE card_id=?", (num["id"], sess["card_id"]))
        security.audit("number_alloc", "info", ip=ip, card_id=sess["card_id"],
                       detail={"phone": num["phone"], "choice": info.get("choice"), "queued": 1})
        ttl = max(1, int(num["allocated_at"] or now) + config.NUMBER_TTL_MIN * 60 - now)
        return ok(number=_num_view(num, ttl))
    if info["state"] in ("error", "expired"):
        return JSONResponse({"ok": False, "error": info.get("error") or "排队失败，请重试",
                             "state": info["state"], "ticket": info}, status_code=409)
    return ok(state="waiting", ahead=info["ahead"], position=info["position"], total=info["total"],
              eta=info["eta"], cooldown=info["cooldown"], queue=nexq.stats())


# ------------------------------------------------------------------ poll
@router.post("/api/poll")
async def poll(request: Request):
    ip = security.client_ip(request)
    sess = security.get_card_session(request)
    if not sess:
        return err("请先验证卡密", 401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    security.require_csrf(request, request.headers.get("x-csrf", "") or body.get("csrf", ""), sess["csrf"])

    allowed, retry = security.hit("poll:%s" % sess["card_id"], 30, 60)
    if not allowed:
        return err("刷新过于频繁", 429, retry_after=retry)

    card = db.q1("SELECT * FROM cards WHERE id=?", (sess["card_id"],))
    if not card:
        return err("卡密不存在", 404)

    existing = db.q1("SELECT * FROM sms_records WHERE card_id=? AND status='received' ORDER BY id DESC LIMIT 1",
                     (card["id"],))
    if existing:
        exhausted = int(card["used_codes"]) >= int(card["max_codes"])
        resp = ok(state="code", code=existing["code"], polls=existing["polls"], done=True,
                  remaining=max(0, int(card["max_codes"]) - int(card["used_codes"])),
                  force_exit=exhausted,
                  grace=max(0, min(60, int(db.get_setting("exit_grace_sec", "8") or 8))))
        if exhausted:
            db.conn().execute("DELETE FROM card_sessions WHERE card_id=?", (card["id"],))
            resp.delete_cookie(config.CARD_COOKIE, path="/")
            resp.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
        resp.headers["Cache-Control"] = "no-store"
        return resp

    num = db.q1("SELECT * FROM numbers WHERE card_id=? AND status='allocated' ORDER BY id DESC LIMIT 1",
                (card["id"],))
    if not num:
        return err("请先获取号码", 409)

    now = int(time.time())
    if num["allocated_at"] + config.NUMBER_TTL_MIN * 60 < now:
        db.conn().execute("UPDATE numbers SET status='expired' WHERE id=?", (num["id"],))
        db.conn().execute("UPDATE sms_records SET status='expired' WHERE number_id=? AND status='waiting'",
                          (num["id"],))
        # Free the one-shot slot here too, otherwise a silent number would keep the
        # card locked until the janitor runs (up to 10 minutes of dead time).
        db.conn().execute("UPDATE card_choices SET number_id=0 WHERE card_id=? AND number_id=?",
                          (card["id"], num["id"]))
        await run_in_threadpool(providers.finish, dict(num), False)
        return err("号码已超时，请重新取号", 410)

    res = await run_in_threadpool(providers.poll, dict(num))
    rec = db.q1("SELECT * FROM sms_records WHERE number_id=? ORDER BY id DESC LIMIT 1", (num["id"],))
    polls = int(rec["polls"]) + 1 if rec else 1

    if res.get("code"):
        code = str(res["code"])
        remaining = 1
        first = False
        with db.tx() as c:
            # The numbers row is the single redemption latch: exactly one concurrent
            # poll can flip allocated -> used, so used_codes can never double-count.
            ncur = c.execute("UPDATE numbers SET status='used', used_at=? "
                             "WHERE id=? AND status='allocated'", (now, num["id"]))
            first = ncur.rowcount == 1
            if rec:
                c.execute("UPDATE sms_records SET code=?, raw=?, polls=?, status='received', got_at=? "
                          "WHERE id=?", (code, (res.get("raw") or "")[:500], polls, now, rec["id"]))
            else:
                c.execute("INSERT INTO sms_records(card_id,number_id,phone,country,code,raw,polls,"
                          "status,created_at,got_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (card["id"], num["id"], num["phone"], num["country"], code,
                           (res.get("raw") or "")[:500], polls, "received", now, now))
            if first:
                c.execute("UPDATE cards SET used_codes=used_codes+1, last_used_at=?, last_ip=? WHERE id=?",
                          (now, ip, card["id"]))
            row = c.execute("SELECT used_codes,max_codes FROM cards WHERE id=?", (card["id"],)).fetchone()
            remaining = max(0, int(row["max_codes"]) - int(row["used_codes"]))
            c.execute("UPDATE cards SET status='used' WHERE id=? AND used_codes>=max_codes", (card["id"],))
            if remaining <= 0:
                # 核销即注销：把这个卡密的所有会话一并销毁，旧 cookie 立刻失效。
                c.execute("DELETE FROM card_sessions WHERE card_id=?", (card["id"],))
        await run_in_threadpool(providers.finish, dict(num), True)
        security.audit("code_received", "info", ip=ip, card_id=card["id"],
                       detail={"phone": num["phone"], "polls": polls, "remaining": remaining})
        force = remaining <= 0
        grace = 8
        try:
            grace = max(0, min(60, int(db.get_setting("exit_grace_sec", "8") or 8)))
        except Exception:
            pass
        resp = ok(state="code", code=code, polls=polls, done=True,
                  remaining=remaining, force_exit=force, grace=grace)
        if force:
            security.audit("card_revoked", "alert", ip=ip, card_id=card["id"],
                           detail={"reason": "code_received", "sessions": "destroyed"})
            resp.delete_cookie(config.CARD_COOKIE, path="/")
            resp.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
        resp.headers["Cache-Control"] = "no-store"
        return resp

    if rec:
        if polls % 5 == 0:
            db.conn().execute("UPDATE sms_records SET polls=?, raw=? WHERE id=?",
                              (polls, (res.get("raw") or "")[:500], rec["id"]))
        else:
            db.conn().execute("UPDATE sms_records SET polls=? WHERE id=?", (polls, rec["id"]))
    if not res.get("ok"):
        security.audit("poll_error", "warn", ip=ip, card_id=card["id"], detail=(res.get("raw") or "")[:200])
    return ok(state="waiting", polls=polls, upstream_ok=bool(res.get("ok")),
              hint="" if res.get("ok") else "上游暂不可达，稍后自动重试")


@router.post("/api/logout")
def logout(request: Request):
    sid = request.cookies.get(config.CARD_COOKIE)
    if sid:
        db.conn().execute("DELETE FROM card_sessions WHERE sid=?", (sid,))
    resp = ok()
    resp.delete_cookie(config.CARD_COOKIE, path="/")
    return resp