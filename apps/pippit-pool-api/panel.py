# -*- coding: utf-8 -*-
"""panel.py - Pippit Pool master panel: QR->TOTP login, dashboard, media auto-sweep."""
import base64, hashlib, hmac, json, os, shutil, struct, threading, time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

import db

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
VIDEOS = os.path.join(DATA, "videos")
ADMIN_TOKEN_FILE = os.path.join(DATA, "admin_token.txt")
HTML_FILE = os.path.join(BASE, "panel.html")

COOKIE = "pp_master"
SESSION_TTL = 7 * 86400
GOOGLE_ISSUER = "Pippit Pool"
router = APIRouter()

_attempts: Dict[str, list] = {}
_attempt_lock = threading.Lock()


# ---------------- encoding / crypto ----------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret() -> bytes:
    s = db.get_setting("panel_secret")
    if not s:
        s = _b64e(os.urandom(32))
        db.set_setting("panel_secret", s)
    return s.encode()


def sign_session(ident: Any = "master", ttl: int = SESSION_TTL) -> str:
    payload = {"k": "master", "id": ident, "exp": int(time.time()) + int(ttl),
               "n": _b64e(os.urandom(8))}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()
    return _b64e(raw) + "." + _b64e(sig)


def verify_session(token: str) -> Optional[dict]:
    try:
        a, b = (token or "").split(".", 1)
        raw = _b64d(a)
        if not hmac.compare_digest(_b64d(b), hmac.new(_secret(), raw, hashlib.sha256).digest()):
            return None
        p = json.loads(raw)
        if p.get("k") != "master" or int(p.get("exp") or 0) < time.time():
            return None
        return p
    except Exception:
        return None


# ---------------- TOTP (RFC 6238) ----------------
def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_at(secret_b32: str, ts: Optional[float] = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((time.time() if ts is None else ts) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret_b32: str, code: str, window: int = 1) -> bool:
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6 or not secret_b32:
        return False
    now = time.time()
    for w in range(-window, window + 1):
        try:
            if hmac.compare_digest(totp_at(secret_b32, now + w * 30), code):
                return True
        except Exception:
            return False
    return False


def otpauth_uri(secret_b32: str, account: str = "master", issuer: str = GOOGLE_ISSUER) -> str:
    """Keep the URI short: SHA1/6/30 are authenticator defaults, so omit them.
    Shorter URI -> lower QR version -> bigger modules -> much easier to scan."""
    from urllib.parse import quote
    label = quote("%s:%s" % (issuer, account), safe="")
    return "otpauth://totp/%s?secret=%s&issuer=%s" % (label, secret_b32, quote(issuer, safe=""))


def qr_png_data_uri(text: str, scale: int = 6, border: int = 4) -> str:
    """Raster QR at an integer module size: v5 symbol (37 modules) + 4-module border
    = 45 units; scale 6 -> 270x270, every module exactly 6x6 device pixels."""
    try:
        import base64 as _b64
        import io
        import segno
        buf = io.BytesIO()
        segno.make(text, error="l").save(buf, kind="png", scale=scale, border=border,
                                          dark="#0b1220", light="#ffffff")
        return "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def qr_svg(text: str, scale: int = 8, border: int = 4) -> str:
    try:
        import io
        import segno
        buf = io.BytesIO()
        segno.make(text, error="l").save(buf, kind="svg", scale=scale, border=border,
                                         dark="#0b1220", light="#ffffff")
        svg = buf.getvalue().decode("utf-8")
        i = svg.find("<svg")
        return svg[i:] if i >= 0 else svg
    except Exception:
        return ""


# ---------------- auth deps ----------------
def _admin_token_ok(request: Request) -> bool:
    tok = request.headers.get("x-admin-token") or ""
    try:
        with open(ADMIN_TOKEN_FILE, "r", encoding="utf-8") as f:
            real = f.read().strip()
        return bool(tok) and hmac.compare_digest(tok, real)
    except Exception:
        return False


def is_master(request: Request) -> bool:
    return verify_session(request.cookies.get(COOKIE) or "") is not None


def require_master(request: Request):
    if is_master(request) or _admin_token_ok(request):
        return True
    raise HTTPException(401, "login required")


def _rate_limit(request: Request):
    ip = (request.client.host if request.client else "?") or "?"
    now = time.time()
    with _attempt_lock:
        arr = [t for t in _attempts.get(ip, []) if now - t < 300]
        if len(arr) >= 12:
            raise HTTPException(429, "too many attempts, wait 5 minutes")
        arr.append(now)
        _attempts[ip] = arr


def _pending_list():
    try:
        lst = json.loads(db.get_setting("master_totp_pending_list") or "[]")
        return [x for x in lst if isinstance(x, dict) and x.get("secret")]
    except Exception:
        return []


def _push_pending(sec: str):
    now = time.time()
    lst = [x for x in _pending_list() if now - float(x.get("ts") or 0) < 1800]
    lst.append({"secret": sec, "ts": now})
    lst = lst[-5:]
    db.set_setting("master_totp_pending_list", json.dumps(lst))
    db.set_setting("master_totp_pending", sec)
    db.set_setting("master_totp_pending_ts", str(now))
    return lst


def _clear_pending():
    db.set_setting("master_totp_pending_list", "[]")
    db.set_setting("master_totp_pending", "")
    db.set_setting("master_totp_pending_ts", "0")


def _set_cookie(resp: Response):
    resp.set_cookie(COOKIE, sign_session("master"), max_age=SESSION_TTL, httponly=True,
                    samesite="lax", path="/")


# ---------------- models ----------------
class CodeReq(BaseModel):
    code: str = Field(..., description="Google Authenticator 当前的 6 位数字", examples=["123456"])


class KeyReq(BaseModel):
    name: str = Field("panel-key", description="Key 名称", examples=["client-a"])
    quota: int = Field(0, description="累计任务额度；0=不限", examples=[0])
    expires_in_days: Optional[int] = Field(None, description="有效天数；null=永久", examples=[30])


class SettingsReq(BaseModel):
    retention_days: Optional[float] = Field(None, description="保留天数；0=不按时间删除", examples=[7])
    max_gb: Optional[float] = Field(None, description="视频总量上限 GB；0=不限", examples=[20])
    enabled: Optional[bool] = Field(None, description="是否启用后台自动清理", examples=[True])


# ---------------- pages / auth ----------------
@router.get("/master", response_class=HTMLResponse, tags=["面板 API"], summary="总台面板页面",
         description="返回总台单页应用。首次访问扫码绑定 TOTP，之后用 6 位验证码登录。")
def master_page():
    try:
        with open(HTML_FILE, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except Exception:
        return HTMLResponse("<h1>panel.html missing</h1>", status_code=500)


@router.get("/master/api/state", tags=["面板 API"], summary="绑定 / 登录状态",
         description="返回是否已完成 TOTP 绑定，以及当前请求是否携带有效登录 Cookie。",
         responses={200: {"description": "enrolled + authed"}})
def master_state(request: Request):
    return {"enrolled": db.get_setting("master_totp_active") == "1",
            "authed": is_master(request)}


@router.post("/master/api/enroll/start", tags=["面板 API"], summary="开始绑定 TOTP",
         description="生成新的 TOTP 密钥，返回 otpauth URI、PNG 二维码与 SVG。最近 5 个 pending 密钥 30 分钟内仍可确认。",
         responses={400: {"description": "已经绑定过"}, 429: {"description": "尝试过于频繁（12 次 / 5 分钟）"}})
def enroll_start(request: Request, fresh: bool = Query(False, description="true=强制生成新密钥；false=复用 30 分钟内的 pending 密钥")):
    _rate_limit(request)
    if db.get_setting("master_totp_active") == "1":
        raise HTTPException(400, "already enrolled")
    sec = db.get_setting("master_totp_pending")
    ts = float(db.get_setting("master_totp_pending_ts") or 0)
    if fresh or not sec or time.time() - ts > 1800:
        sec = new_totp_secret()
        _push_pending(sec)
    else:
        _push_pending(sec)
    uri = otpauth_uri(sec)
    return {"secret": sec, "otpauth": uri, "qr_svg": qr_svg(uri),
            "qr_png": qr_png_data_uri(uri),
            "issuer": GOOGLE_ISSUER, "account": "master"}


@router.post("/master/api/enroll/confirm", tags=["面板 API"], summary="确认绑定 TOTP",
         description="校验 6 位验证码；成功后写入 master_totp_secret 并下发 7 天 HttpOnly 登录 Cookie。",
         responses={400: {"description": "没有 pending 绑定或验证码错误"}, 429: {"description": "尝试过于频繁"}})
def enroll_confirm(req: CodeReq, request: Request, resp: Response):
    _rate_limit(request)
    candidates = [x["secret"] for x in _pending_list()]
    legacy = db.get_setting("master_totp_pending")
    if legacy and legacy not in candidates:
        candidates.append(legacy)
    if not candidates:
        raise HTTPException(400, "no pending enrollment")
    matched = None
    for sec in candidates:
        if verify_totp(sec, req.code):
            matched = sec
            break
    if not matched:
        raise HTTPException(400, "code mismatch, check your authenticator clock")
    db.set_setting("master_totp_secret", matched)
    db.set_setting("master_totp_active", "1")
    _clear_pending()
    _set_cookie(resp)
    return {"ok": True}


@router.post("/master/api/login", tags=["面板 API"], summary="验证码登录",
         description="用 Google Authenticator 当前 6 位码登录，成功下发 7 天 HttpOnly Cookie。",
         responses={400: {"description": "尚未绑定或验证码错误"}, 429: {"description": "尝试过于频繁"}})
def master_login(req: CodeReq, request: Request, resp: Response):
    _rate_limit(request)
    sec = db.get_setting("master_totp_secret")
    if db.get_setting("master_totp_active") != "1" or not sec:
        raise HTTPException(400, "not enrolled yet")
    if not verify_totp(sec, req.code):
        raise HTTPException(400, "invalid code")
    _set_cookie(resp)
    return {"ok": True}


@router.post("/master/api/logout", tags=["面板 API"], summary="退出登录",
         description="清除 pp_master Cookie。")
def master_logout(resp: Response):
    resp.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ---------------- dashboard data ----------------
@router.get("/master/api/overview", tags=["面板 API"], summary="看板总览",
         description="返回账号数、积分、可产条数、任务统计、视频占用、磁盘与清理状态。",
         responses={401: {"description": "未登录或 Cookie 过期"}})
def overview(_=Depends(require_master)):
    acc = db.queryone("SELECT COUNT(*) n, COALESCE(SUM(credit),0) credit FROM accounts") or {}
    by_status = {r["status"]: int(r["n"]) for r in
                 db.query("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    t0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    today = db.queryone("SELECT COUNT(*) n FROM jobs WHERE status='completed' AND completed_at>=?",
                        (t0,)) or {}
    vids = db.query("SELECT video_path FROM jobs WHERE video_path IS NOT NULL AND video_path<>''")
    vbytes, vcount, missing = 0, 0, 0
    for v in vids:
        p = v["video_path"]
        try:
            vbytes += os.path.getsize(p)
            vcount += 1
        except Exception:
            missing += 1
    try:
        du = shutil.disk_usage(DATA)
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except Exception:
        disk = {"total": 0, "used": 0, "free": 0}
    keys = db.queryone("SELECT COUNT(*) n FROM api_keys WHERE active=1") or {}
    charge = db.query("SELECT credit FROM accounts WHERE status IN ('ready','busy')")
    capacity = sum(int((r.get("credit") or 0) // 120) for r in charge)
    chargeable = sum(1 for r in charge if int(r.get("credit") or 0) >= 120)
    return {
        "capacity": capacity,
        "chargeable_accounts": chargeable,
        "accounts": int(acc.get("n") or 0),
        "credits": int(acc.get("credit") or 0),
        "accounts_ready": int((db.queryone(
            "SELECT COUNT(*) n FROM accounts WHERE status='ready'") or {}).get("n") or 0),
        "jobs": by_status,
        "completed_today": int(today.get("n") or 0),
        "videos": {"count": vcount, "bytes": vbytes, "missing": missing},
        "disk": disk,
        "keys": int(keys.get("n") or 0),
        "sweep": {
            "enabled": db.get_setting("media_sweep_enabled", "1") == "1",
            "retention_days": float(db.get_setting("media_retention_days", "7") or 7),
            "max_gb": float(db.get_setting("media_max_gb", "20") or 20),
            "last_ts": float(db.get_setting("media_last_sweep_ts", "0") or 0),
            "last_freed": int(float(db.get_setting("media_last_sweep_freed", "0") or 0)),
            "last_deleted": int(float(db.get_setting("media_last_sweep_deleted", "0") or 0)),
        },
        "server_time": int(time.time()),
    }


@router.get("/master/api/jobs", tags=["面板 API"], summary="任务列表",
         description="按创建时间倒序返回任务，可按状态过滤，最多 300 条。",
         responses={401: {"description": "未登录或 Cookie 过期"}})
def jobs(limit: int = Query(50, ge=1, le=300, description="返回条数，1–300"),
         status: Optional[str] = Query(None, description="queued / running / completed / failed / canceled"),
         _=Depends(require_master)):
    limit = max(1, min(int(limit), 300))
    if status:
        rows = db.query("SELECT j.*, k.name key_name FROM jobs j LEFT JOIN api_keys k ON k.id=j.key_id "
                        "WHERE j.status=? ORDER BY j.created_at DESC LIMIT ?", (status, limit))
    else:
        rows = db.query("SELECT j.*, k.name key_name FROM jobs j LEFT JOIN api_keys k ON k.id=j.key_id "
                        "ORDER BY j.created_at DESC LIMIT ?", (limit,))
    for r in rows:
        r.pop("prompt_full", None)
    return {"object": "list", "data": rows}


@router.get("/master/api/jobs/{jid}", tags=["面板 API"], summary="任务详情 + 事件",
         description="返回任务完整字段、最近 40 条事件，以及本地视频是否仍存在。",
         responses={401: {"description": "未登录"}, 404: {"description": "任务不存在"}})
def job_detail(jid: str = Path(..., description="任务 ID"), _=Depends(require_master)):
    j = db.queryone("SELECT j.*, k.name key_name FROM jobs j LEFT JOIN api_keys k ON k.id=j.key_id "
                    "WHERE j.id=?", (jid,))
    if not j:
        raise HTTPException(404, "job not found")
    j["events"] = db.query("SELECT ts,msg FROM events WHERE job_id=? ORDER BY ts DESC LIMIT 40", (jid,))
    j["video_exists"] = bool(j.get("video_path") and os.path.exists(j["video_path"]))
    return j


@router.get("/master/api/jobs/{jid}/video", tags=["面板 API"], summary="播放本地成片",
         description="在面板中直接播放服务器本地 mp4。文件已被清理时返回 404。",
         responses={401: {"description": "未登录"}, 404: {"description": "视频文件不可用"}})
def job_video(jid: str = Path(..., description="任务 ID"), _=Depends(require_master)):
    j = db.get_job(jid)
    if not j or not j.get("video_path") or not os.path.exists(j["video_path"]):
        raise HTTPException(404, "video file not available")
    return FileResponse(j["video_path"], media_type="video/mp4", filename=os.path.basename(j["video_path"]))


@router.get("/master/api/keys", tags=["面板 API"], summary="Key 列表",
         description="返回全部 Key 的元数据（不含明文）。",
         responses={401: {"description": "未登录"}})
def keys(_=Depends(require_master)):
    return {"object": "list", "data": db.list_keys()}


@router.post("/master/api/keys", tags=["面板 API"], summary="创建 Key",
         description="创建新的 Bearer Key；明文只在本次响应返回。",
         responses={401: {"description": "未登录"}})
def key_new(req: KeyReq, _=Depends(require_master)):
    kid, raw = db.create_key(req.name, req.quota, req.expires_in_days)
    return {"id": kid, "key": raw, "name": req.name, "quota": req.quota,
            "expires_in_days": req.expires_in_days, "note": "store now; shown once"}


@router.delete("/master/api/keys/{kid}", tags=["面板 API"], summary="撤销 Key",
         description="将 Key 标记为失效，历史任务保留。",
         responses={401: {"description": "未登录"}})
def key_del(kid: str = Path(..., description="Key ID"), _=Depends(require_master)):
    db.revoke_key(kid)
    return {"id": kid, "active": False}


@router.get("/master/api/accounts", tags=["面板 API"], summary="账号列表",
         description="按积分倒序返回账号状态、冷却时间、成功/失败计数，最多 500 条。",
         responses={401: {"description": "未登录"}})
def accounts(limit: int = Query(200, ge=1, le=500, description="返回条数，1–500"), _=Depends(require_master)):
    rows = db.query("SELECT uid,tag,credit,status,cooldown_until,last_error,jobs_done,jobs_failed,"
                    "last_used,created_at FROM accounts ORDER BY credit DESC, last_used ASC LIMIT ?",
                    (max(1, min(int(limit), 500)),))
    return {"object": "list", "count": len(rows), "data": rows}


@router.get("/master/api/settings", tags=["面板 API"], summary="读取清理设置",
         description="返回保留天数、总量上限与自动清理开关。",
         responses={401: {"description": "未登录"}})
def settings_get(_=Depends(require_master)):
    return {"retention_days": float(db.get_setting("media_retention_days", "7") or 7),
            "max_gb": float(db.get_setting("media_max_gb", "20") or 20),
            "enabled": db.get_setting("media_sweep_enabled", "1") == "1"}


@router.post("/master/api/settings", tags=["面板 API"], summary="保存清理设置",
         description="更新保留天数、总量上限与自动清理开关；返回保存后的完整设置。",
         responses={401: {"description": "未登录"}})
def settings_set(req: SettingsReq, _=Depends(require_master)):
    if req.retention_days is not None:
        db.set_setting("media_retention_days", max(0.0, float(req.retention_days)))
    if req.max_gb is not None:
        db.set_setting("media_max_gb", max(0.0, float(req.max_gb)))
    if req.enabled is not None:
        db.set_setting("media_sweep_enabled", "1" if req.enabled else "0")
    return settings_get()


@router.post("/master/api/sweep", tags=["面板 API"], summary="执行 / 预览清理",
         description="按保留天数、总量上限与低磁盘策略扫描视频。dry_run=true 只预览，不删除文件。",
         responses={401: {"description": "未登录"}})
def sweep_now(dry_run: bool = Query(True, description="true=只预览；false=真正删除"), _=Depends(require_master)):
    return sweep(dry_run=dry_run)


# ---------------- media sweep ----------------
def sweep(dry_run: bool = True, force: bool = False) -> dict:
    """Delete finished videos by age / total size / low disk. Safe to call often."""
    days = float(db.get_setting("media_retention_days", "7") or 7)
    max_gb = float(db.get_setting("media_max_gb", "20") or 20)
    cap = max_gb * 1024 ** 3 if max_gb > 0 else 0
    rows = db.query("SELECT id, video_path, bytes, completed_at FROM jobs "
                    "WHERE video_path IS NOT NULL AND video_path<>'' ORDER BY completed_at ASC")
    files = []
    total = 0
    for r in rows:
        p = r["video_path"]
        if not p or not os.path.exists(p):
            if not dry_run:
                db.update_job(r["id"], video_path=None)
            continue
        try:
            sz = os.path.getsize(p)
        except Exception:
            sz = 0
        files.append((r, sz))
        total += sz

    now = time.time()
    deleted = []
    freed = 0
    for r, sz in files:
        age = (now - float(r.get("completed_at") or now)) / 86400.0
        too_old = days > 0 and age > days
        over_cap = cap > 0 and total > cap
        if not (too_old or over_cap):
            continue
        if not dry_run:
            try:
                os.remove(r["video_path"])
            except FileNotFoundError:
                pass
            except Exception as e:
                db.add_event(r["id"], "media sweep failed: %r" % (e,))
                continue
            db.update_job(r["id"], video_path=None, media_deleted_at=now)
            db.add_event(r["id"], "media swept age=%.2fd size=%d" % (age, sz))
        total -= sz
        freed += sz
        deleted.append({"id": r["id"], "bytes": sz, "age_days": round(age, 2)})

    try:
        free = shutil.disk_usage(DATA).free
    except Exception:
        free = 0
    low_disk = False
    if free and free < 5 * 1024 ** 3:
        low_disk = True
        for r, sz in files:
            if free > 8 * 1024 ** 3:
                break
            if any(d["id"] == r["id"] for d in deleted):
                continue
            if not os.path.exists(r["video_path"]):
                continue
            if not dry_run:
                try:
                    os.remove(r["video_path"])
                    db.update_job(r["id"], video_path=None, media_deleted_at=now)
                    db.add_event(r["id"], "media swept low-disk size=%d" % sz)
                except Exception:
                    continue
            free += sz
            freed += sz
            deleted.append({"id": r["id"], "bytes": sz, "age_days": None})

    if not dry_run:
        db.set_setting("media_last_sweep_ts", str(now))
        db.set_setting("media_last_sweep_freed", str(freed))
        db.set_setting("media_last_sweep_deleted", str(len(deleted)))
    return {"dry_run": bool(dry_run), "scanned": len(files), "deleted_count": len(deleted),
            "freed_bytes": freed, "remaining_bytes": total, "free_disk": free,
            "low_disk": low_disk, "retention_days": days, "max_gb": max_gb,
            "items": deleted[:50]}


_thread = None


def start_cleanup(interval: int = 1800):
    global _thread
    if os.environ.get("WZ_MEDIA_SWEEP", "1") == "0":
        return
    if _thread and _thread.is_alive():
        return

    def loop():
        time.sleep(45)
        while True:
            try:
                if db.get_setting("media_sweep_enabled", "1") == "1":
                    r = sweep(dry_run=False)
                    if r.get("deleted_count"):
                        print("[sweep] deleted=%d freed=%.1fMB free=%.1fGB" % (
                            r["deleted_count"], r["freed_bytes"] / 1048576.0,
                            r["free_disk"] / 1073741824.0))
            except Exception as e:
                print("[sweep] error %r" % (e,))
            time.sleep(max(60, int(interval)))

    _thread = threading.Thread(target=loop, daemon=True, name="media-sweep")
    _thread.start()
    return _thread
