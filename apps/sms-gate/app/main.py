# -*- coding: utf-8 -*-
"""SMS Gate - FastAPI application."""
from __future__ import annotations
import asyncio, threading, time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config, db, nexq, providers, public_api, admin_api, relay_api, security

app = FastAPI(title="SMS Gate", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(public_api.router)
app.include_router(admin_api.router, prefix="/admin")
app.include_router(relay_api.router)

env = Environment(loader=FileSystemLoader(str(config.TEMPLATE_DIR)),
                  autoescape=select_autoescape(["html", "xml"]))
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'; object-src 'none'"),
}


@app.on_event("startup")
def _startup() -> None:
    db.init()
    security.purge_expired()
    # Queue is retired: everyone allocates directly; leftovers are closed once.
    nexq.retire_queue()
    try:
        _expire_stale_numbers()
    except Exception:
        pass
    # 100+ concurrent visitors: give the blocking provider calls enough threads.
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = 80
    except Exception:
        pass
    # Warm the NexSMS project cache so the first visitor does not pay the scan cost.
    threading.Thread(target=providers.warm_nex_cache, daemon=True).start()


def _expire_stale_numbers() -> None:
    """Free numbers (and their one-shot slots) that timed out without a code."""
    now = int(time.time())
    ttl = config.NUMBER_TTL_MIN * 60
    rows = db.q("SELECT id,card_id FROM numbers WHERE status='allocated' "
                "AND allocated_at>0 AND allocated_at<?", (now - ttl,))
    for r in rows:
        db.conn().execute("UPDATE numbers SET status='expired' WHERE id=?", (r["id"],))
        db.conn().execute("UPDATE sms_records SET status='expired' "
                          "WHERE number_id=? AND status='waiting'", (r["id"],))
        if r["card_id"]:
            db.conn().execute("UPDATE card_choices SET number_id=0 "
                              "WHERE card_id=? AND number_id=?", (r["card_id"], r["id"]))


async def _janitor():
    while True:
        await asyncio.sleep(600)
        try:
            security.purge_expired()
        except Exception:
            pass
        try:
            await asyncio.to_thread(_expire_stale_numbers)
        except Exception:
            pass


@app.on_event("startup")
async def _spawn_janitor():
    asyncio.create_task(_janitor())


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static/"):
        return await call_next(request)
    ip = security.client_ip(request)
    if path != "/api/health":
        until, reason = security.blocked_until(ip)
        if until and until > time.time():
            security.audit("blocked_hit", "warn", ip=ip, detail={"path": path, "reason": reason})
            if path.startswith("/api/") or path.startswith("/admin/api/"):
                return JSONResponse({"ok": False, "error": "IP 已被临时封禁", "blocked": True}, status_code=403)
            return PlainTextResponse("403 Forbidden - IP temporarily blocked", status_code=403)
        allowed, retry = security.hit("global:%s" % ip, 600, 60)
        if not allowed:
            security.touch_ip(ip, failed=True)
            return JSONResponse({"ok": False, "error": "请求过于频繁", "retry_after": retry}, status_code=429)
        security.touch_ip(ip)
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if path.startswith("/api/") or path.startswith("/admin/api/"):
        # Codes, tickets and session payloads must never sit in a cache.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
    if security.is_https(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    security.audit("unhandled_error", "error", ip=security.client_ip(request),
                   detail="%s: %s" % (type(exc).__name__, str(exc)[:200]))
    if request.url.path.startswith("/api") or request.url.path.startswith("/admin/api"):
        return JSONResponse({"ok": False, "error": "服务异常，请稍后重试"}, status_code=500)
    return PlainTextResponse("500 Internal Server Error", status_code=500)


def render(name: str, request: Request, **ctx) -> HTMLResponse:
    tpl = env.get_template(name)
    base = {
        "site_name": db.get_setting("site_name", config.SITE_NAME),
        "note": db.get_setting("site_note", ""),
        "poll_interval": db.get_setting("poll_interval", "15"),
        "shop_url": db.get_setting("shop_url", "https://wzyp.cn/shop/63EZ1CJK"),
        "year": time.strftime("%Y"),
    }
    base.update(ctx)
    return HTMLResponse(tpl.render(**base))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Marketing landing page. The card entry lives at /redeem."""
    return render("landing.html", request)


@app.get("/redeem", response_class=HTMLResponse)
def redeem_page(request: Request):
    return render("public.html", request)


@app.get("/c/{key}", response_class=HTMLResponse)
def card_link(key: str, request: Request):
    return render("public.html", request, card_key=key)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    return render("admin.html", request)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request):
    s = security.get_admin_session(request)
    if s and s.get("stage") == "active":
        return RedirectResponse("/admin", status_code=302)
    return render("login.html", request)


@app.get("/admin/setup", response_class=HTMLResponse)
def admin_setup(request: Request):
    return render("setup.html", request)


@app.get("/favicon.ico")
def favicon():
    return PlainTextResponse("", status_code=204)