# -*- coding: utf-8 -*-
"""Pippit pool API - OpenAPI/FastAPI front for the 1080p/30s video pipeline.

Auth:  Authorization: Bearer sk-pp-...        (client keys)
Admin: X-Admin-Token: <token from logs/apipool/admin_token.txt>
Docs:  /docs  /openapi.json
"""
import io, json, os, secrets, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
C = r"<PROJECT_ROOT>"
sys.path.insert(0, os.path.join(C, "work")); sys.path.insert(0, HERE)
import db  # noqa: E402
import totp as totp_mod  # noqa: E402
try:
    # pool/worker only exist on the render box; a control-plane-only deployment
    # (API_WORKERS=0) must still import cleanly on Linux.
    import pool  # noqa: E402
except Exception:
    pool = None
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

TOKEN_FILE = os.path.join(HERE, "admin_token.txt")
# Public prefix when served behind nginx sub-path (e.g. /pippit).
ROOT_PATH = (os.environ.get("API_ROOT_PATH", "") or "").strip().rstrip("/")
def abs_url(path):
    return (ROOT_PATH + path) if ROOT_PATH else path
def admin_token():
    t = os.environ.get("API_ADMIN_TOKEN", "").strip()
    if t: return t
    try:
        t = io.open(TOKEN_FILE, encoding="utf-8").read().strip()
    except Exception:
        t = ""
    if not t:
        t = "adm-" + secrets.token_urlsafe(28)
        io.open(TOKEN_FILE, "w", encoding="utf-8").write(t)
    return t

app = FastAPI(
    title="Pippit Pool API",
    version="1.0.0",
    description=(
        "Account-pool backed video generation API.\n\n"
        "Output contract: **1080x1920 (9:16), 30.000s, 900 frames, no watermark**.\n\n"
        "Create a key with `POST /v1/keys` (admin), then call `POST /v1/videos/generations` with "
        "`Authorization: Bearer sk-pp-...`. Poll `GET /v1/videos/generations/{id}` and download from "
        "`GET /v1/videos/generations/{id}/content`."
    ),
    root_path=ROOT_PATH or None,
)

def key_auth(authorization: str = Header(default="")):
    tok = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    k = db.find_key(tok)
    if not k:
        raise HTTPException(401, "invalid api key")
    return k

SESSION_COOKIE = "wz_admin"
SESSION_TTL = int(os.environ.get("API_ADMIN_SESSION_TTL", "43200"))

def admin_auth(request: Request, x_admin_token: str = Header(default="")):
    """Accept either the bootstrap admin token or a TOTP-backed session cookie."""
    if x_admin_token and secrets.compare_digest(x_admin_token, admin_token()):
        return True
    if db.admin_session_valid(request.cookies.get(SESSION_COOKIE, "")):
        return True
    raise HTTPException(401, "admin auth required: X-Admin-Token or /admin TOTP login")

class KeyCreate(BaseModel):
    name: str = Field(default="default", description="label for the key")
    quota_credits: int = Field(default=0, ge=0, description="0 = unlimited; 120 credits = one 30s 1080p clip")

class GenRequest(BaseModel):
    prompt: str = Field(..., min_length=4, description="scene description; the pipeline appends format constraints")
    model: str = Field(default="seedance-2.5")
    resolution: str = Field(default="1080p", description="1080p only (deliverable contract)")
    duration: float = Field(default=30, ge=5, le=30)
    ratio: str = Field(default="9:16")

JOB_TIMEOUT = int(os.environ.get("API_JOB_TIMEOUT", "5400"))


RETENTION_DAYS = int(os.environ.get("API_RETENTION_DAYS", "14"))


def _cleanup_outputs():
    """Keep the content volume bounded: drop result files older than N days."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    try:
        names = os.listdir(OUT_DIR)
    except Exception:
        return 0
    for fn in names:
        if not fn.endswith(".mp4"):
            continue
        fp = os.path.join(OUT_DIR, fn)
        try:
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                removed += 1
        except Exception:
            pass
    return removed


def _reaper():
    """Fail jobs whose worker vanished mid-render so clients never wait forever."""
    last_clean = 0.0  # first pass runs on the first tick
    while True:
        try:
            n = db.reap_stuck(JOB_TIMEOUT)
            if n:
                print("reaper: failed %d stuck job(s)" % n, flush=True)
        except Exception as e:
            print("reaper err:", repr(e)[:160], flush=True)
        if RETENTION_DAYS > 0 and time.time() - last_clean > 3600:
            try:
                gone = _cleanup_outputs()
                if gone:
                    print("retention: removed %d file(s) older than %dd" % (gone, RETENTION_DAYS), flush=True)
            except Exception as e:
                print("retention err:", repr(e)[:160], flush=True)
            last_clean = 0.0  # first pass runs on the first tick
        time.sleep(60)


@app.on_event("startup")
def _start():
    db.init()
    db.init_admin()
    if os.environ.get("API_REAPER", "1") != "0":
        try:
            import threading
            threading.Thread(target=_reaper, name="job-reaper", daemon=True).start()
            print("job reaper: timeout=%ds" % JOB_TIMEOUT, flush=True)
        except Exception as e:
            print("reaper start failed:", repr(e)[:120], flush=True)
    if os.environ.get("API_DISABLE_WORKERS") != "1":
        try:
            import worker
            worker.start()
        except Exception as e:
            print("worker start failed:", repr(e)[:200], flush=True)
    print("admin token file:", TOKEN_FILE, flush=True)

@app.get("/healthz", tags=["system"])
def healthz():
    return {"ok": True, "service": "pippit-pool-api", "time": time.time()}

@app.get("/v1/models", tags=["system"])
def models():
    return {"object": "list", "data": [
        {"id": "seedance-2.5", "object": "model", "owned_by": "pippit", "resolution": "1080p", "max_duration": 30},
        {"id": "seedance-2.0", "object": "model", "owned_by": "pippit", "resolution": "1080p", "max_duration": 30},
    ]}

@app.post("/v1/keys", tags=["admin"], dependencies=[Depends(admin_auth)])
def create_key(body: KeyCreate):
    """Create an API key. The plaintext key is returned only once."""
    return db.create_key(body.name, body.quota_credits)

@app.get("/v1/keys", tags=["admin"], dependencies=[Depends(admin_auth)])
def get_keys():
    return {"object": "list", "data": db.list_keys()}

@app.delete("/v1/keys/{kid}", tags=["admin"], dependencies=[Depends(admin_auth)])
def delete_key(kid: str):
    if not db.revoke_key(kid):
        raise HTTPException(404, "key not found")
    return {"id": kid, "active": False}

def job_view(j):
    prog = 0
    if j["status"] == "succeeded": prog = 100
    elif j["status"] == "running" and j.get("started_at"):
        prog = min(95, int((time.time() - j["started_at"]) / 600.0 * 100))
    out = {
        "id": j["id"], "object": "video.generation", "created": int(j["created_at"]),
        "status": j["status"], "model": j["model"], "prompt": j["prompt"],
        "resolution": j["resolution"], "duration": j["duration"], "ratio": j["ratio"],
        "progress": prog, "error": j.get("error"),
    }
    if j["status"] == "succeeded":
        out["result"] = {"url": abs_url("/v1/videos/generations/%s/content" % j["id"]), "content_type": "video/mp4"}
    return out

@app.post("/v1/videos/generations", tags=["videos"], status_code=202)
def create_video(body: GenRequest, k=Depends(key_auth)):
    if body.resolution not in ("1080p", "1080x1920"):
        raise HTTPException(400, "resolution must be 1080p (deliverable contract)")
    if body.ratio not in ("9:16", "9x16"):
        raise HTTPException(400, "ratio must be 9:16")
    if body.model not in ("seedance-2.5", "seedance-2.0"):
        raise HTTPException(400, "unknown model")
    # Deliverable contract is fixed at 30.000s: short custom durations are not honoured
    # by the platform (a 6s request rendered 30s and still charged 120 credits), so we
    # refuse anything else instead of quoting a price we cannot deliver.
    if not (29.5 <= float(body.duration) <= 30.5):
        raise HTTPException(400, "duration must be 30 (deliverable contract is 1080p/30.000s)")
    dur = 30.0
    cost = 120
    if k["quota_credits"] > 0 and k["used_credits"] + cost > k["quota_credits"]:
        raise HTTPException(402, "quota exceeded: need %d credits, remaining %d" % (cost, max(0, k["quota_credits"] - k["used_credits"])))
    jid = "vid_" + secrets.token_hex(8)
    db.add_job(jid, k["id"], body.prompt.strip(), body.model, "1080p", dur, "9:16", cost)
    return job_view(db.get_job(jid))

@app.get("/v1/videos/generations", tags=["videos"])
def list_generations(limit: int = Query(default=20, ge=1, le=100), k=Depends(key_auth)):
    rows = db.list_jobs(k["id"], limit)
    return {"object": "list", "data": [job_view(j) for j in rows]}

@app.get("/v1/videos/generations/{jid}", tags=["videos"])
def get_generation(jid: str, k=Depends(key_auth)):
    j = db.get_job(jid)
    if not j or j["key_id"] != k["id"]:
        raise HTTPException(404, "job not found")
    return job_view(j)

@app.post("/v1/videos/generations/{jid}/cancel", tags=["videos"])
def cancel_generation(jid: str, k=Depends(key_auth)):
    j = db.get_job(jid)
    if not j or j["key_id"] != k["id"]:
        raise HTTPException(404, "job not found")
    if j["status"] in ("succeeded", "failed", "canceled"):
        return job_view(j)
    db.request_cancel(jid)
    return {"id": jid, "cancel_requested": True, "note": "running jobs stop after the current render"}

@app.get("/v1/videos/generations/{jid}/content", tags=["videos"])
def get_content(jid: str, k=Depends(key_auth)):
    j = db.get_job(jid)
    if not j or j["key_id"] != k["id"]:
        raise HTTPException(404, "job not found")
    if j["status"] != "succeeded" or not j.get("result_path") or not os.path.exists(j["result_path"]):
        raise HTTPException(409, "not ready")
    return FileResponse(j["result_path"], media_type="video/mp4", filename=jid + ".mp4")

@app.head("/v1/videos/generations/{jid}/content", tags=["videos"])
def head_content(jid: str, k=Depends(key_auth)):
    """Same checks as GET; lets clients probe size/availability without downloading."""
    return get_content(jid, k)

@app.get("/v1/usage", tags=["account"])
def usage(k=Depends(key_auth)):
    remain = None if k["quota_credits"] == 0 else max(0, k["quota_credits"] - k["used_credits"])
    return {"key_id": k["id"], "name": k["name"], "quota_credits": k["quota_credits"],
            "used_credits": k["used_credits"], "remaining_credits": remain,
            "equivalent_30s_clips": None if remain is None else remain // 120}

@app.get("/v1/pool/status", tags=["admin"], dependencies=[Depends(admin_auth)])
def pool_status():
    st = pool.stats() if pool is not None else {"error": "pool not available on this host (control-plane mode)"}
    st["jobs"] = db.counts()
    return st

# ---------------------------------------------------------------------------
# Remote worker protocol: the server stays the public control plane; a worker on
# the machine that owns Chrome/proxies/accounts claims jobs and uploads results.
# ---------------------------------------------------------------------------
WORKER_TOKEN = os.environ.get("API_WORKER_TOKEN", "").strip() or admin_token()
OUT_DIR = os.environ.get("API_OUT_DIR", r"<OUTPUT_DIR>\api")
os.makedirs(OUT_DIR, exist_ok=True)


def worker_auth(x_worker_token: str = Header(default="")):
    if x_worker_token != WORKER_TOKEN:
        raise HTTPException(401, "invalid worker token")
    return True


@app.post("/v1/worker/heartbeat", tags=["worker"], dependencies=[Depends(worker_auth)])
def worker_heartbeat(body: dict):
    """Lets the console show whether a render box is actually online."""
    wid = (str(body.get("wid") or "remote")[:40]) or "remote"
    db.kv_set("worker_hb:" + wid, json.dumps({
        "ts": time.time(), "info": body.get("info") or {},
    }))
    return {"ok": True, "server_time": time.time()}


@app.post("/v1/worker/claim", tags=["worker"], dependencies=[Depends(worker_auth)])
def worker_claim(wid: str = Query(default="remote")):
    jid = db.claim_job("remote:" + wid)
    if not jid:
        return {"job": None}
    j = db.get_job(jid)
    return {"job": {k: j[k] for k in ("id", "prompt", "model", "resolution", "duration", "ratio", "attempts")}}


@app.post("/v1/worker/{jid}/result", tags=["worker"], dependencies=[Depends(worker_auth)])
async def worker_result(jid: str, file: UploadFile = File(...), account_uid: str = Form(default="")):
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404, "job not found")
    dest = os.path.join(OUT_DIR, jid + ".mp4")
    with open(dest, "wb") as f:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    size = os.path.getsize(dest)
    cost = int(j["cost_credits"] or 120)
    db.finish_job(jid, "succeeded", result_path=dest, account_uid=account_uid, tag="REMOTE", cost=cost)
    db.bump_usage(j["key_id"], cost)
    return {"ok": True, "size": size}


@app.post("/v1/worker/{jid}/release", tags=["worker"], dependencies=[Depends(worker_auth)])
def worker_release(jid: str):
    """Hand a job back to the queue when the pool has no affordable account.

    Does not count as an attempt, so a temporarily dry pool never kills the job.
    """
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404, "job not found")
    db.release_job(jid)
    return {"ok": True, "released": True, "id": jid}


@app.post("/v1/worker/{jid}/fail", tags=["worker"], dependencies=[Depends(worker_auth)])
def worker_fail(jid: str, body: dict):
    j = db.get_job(jid)
    if not j:
        raise HTTPException(404, "job not found")
    err = str(body.get("error") or "remote fail")[:400]
    if int(j["attempts"] or 0) < 3:
        db.requeue_job(jid, err)
        return {"ok": True, "requeued": True}
    db.finish_job(jid, "failed", error=err)
    return {"ok": True, "requeued": False}


# ---------------------------------------------------------------------------
# Admin console: Google Authenticator (TOTP) enrolment + 6-digit login
# ---------------------------------------------------------------------------
ADMIN_HTML = os.path.join(HERE, "admin.html")
_TOTP_KEY = "admin_totp_secret"
_PENDING = {"secret": "", "at": 0.0}
_FAILS = {}          # ip -> [timestamps]
_TRIES = {}          # ip -> [timestamps]


def _ip(request):
    xf = request.headers.get("x-forwarded-for", "")
    return (xf.split(",")[0].strip() or (request.client.host if request.client else "?"))


def _throttled(ip, bucket, limit, window):
    now = time.time()
    arr = [t for t in bucket.get(ip, []) if now - t < window]
    bucket[ip] = arr
    if len(arr) >= limit:
        return True
    arr.append(now)
    return False


def _set_session(response, request):
    tok = db.admin_session_create(SESSION_TTL, _ip(request))
    response.set_cookie(SESSION_COOKIE, tok, max_age=SESSION_TTL, httponly=True,
                        samesite="lax", path=(ROOT_PATH or "/"))
    return tok


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
def admin_page():
    # no-store so a cached copy of the page never talks to a stale API path
    return FileResponse(ADMIN_HTML, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/admin/api/status", include_in_schema=False)
def admin_status(request: Request):
    return {"enrolled": bool(db.kv_get(_TOTP_KEY)),
            "authed": db.admin_session_valid(request.cookies.get(SESSION_COOKIE, "")),
            "root_path": ROOT_PATH}


@app.post("/admin/api/enroll/start", include_in_schema=False)
def admin_enroll_start(request: Request, body: dict | None = None,
                       x_admin_token: str = Header(default="")):
    """Create a pending TOTP secret and return the otpauth QR for Google Authenticator."""
    tok_ok = bool(x_admin_token) and secrets.compare_digest(x_admin_token, admin_token())
    sess_ok = db.admin_session_valid(request.cookies.get(SESSION_COOKIE, ""))
    if not (tok_ok or sess_ok):
        raise HTTPException(401, "admin token required to start enrolment")
    force = bool((body or {}).get("force"))
    if db.kv_get(_TOTP_KEY) and not force:
        raise HTTPException(409, "already enrolled; pass force=true to re-bind")
    secret = totp_mod.new_secret()
    _PENDING["secret"] = secret
    _PENDING["at"] = time.time()
    uri = totp_mod.otpauth_uri(secret)
    return {"secret": secret, "otpauth": uri, "qr": totp_mod.qr_svg_data_uri(uri),
            "expires_in": 600}


@app.post("/admin/api/enroll/confirm", include_in_schema=False)
def admin_enroll_confirm(body: dict, request: Request, response: Response):
    secret = _PENDING.get("secret") or ""
    if not secret or time.time() - _PENDING.get("at", 0) > 600:
        raise HTTPException(400, "no pending enrolment (start again)")
    ip = _ip(request)
    if _throttled(ip, _TRIES, 12, 300):
        raise HTTPException(429, "too many attempts, wait a few minutes")
    code = str(body.get("code", ""))
    if not totp_mod.verify(secret, code):
        raise HTTPException(401, "invalid code")
    db.kv_set(_TOTP_KEY, secret)
    db.admin_sessions_clear()
    _PENDING["secret"] = ""
    _set_session(response, request)
    return {"ok": True, "enrolled": True}


@app.post("/admin/api/login", include_in_schema=False)
def admin_login(body: dict, request: Request, response: Response):
    ip = _ip(request)
    if _throttled(ip, _FAILS, 10, 300):
        raise HTTPException(429, "too many failed logins, wait 5 minutes")
    secret = db.kv_get(_TOTP_KEY)
    if not secret:
        raise HTTPException(400, "not enrolled yet")
    code = str(body.get("code", ""))
    if not totp_mod.verify(secret, code):
        raise HTTPException(401, "invalid code")
    db.admin_session_create  # keep import surface explicit
    _set_session(response, request)
    _FAILS.pop(ip, None)
    return {"ok": True}


@app.post("/admin/api/logout", include_in_schema=False)
def admin_logout(request: Request, response: Response):
    db.admin_session_destroy(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path=(ROOT_PATH or "/"))
    return {"ok": True}


@app.post("/admin/api/reset", include_in_schema=False)
def admin_reset(request: Request, x_admin_token: str = Header(default="")):
    """Recovery: wipe TOTP secret + sessions (requires the bootstrap admin token)."""
    if not (x_admin_token and secrets.compare_digest(x_admin_token, admin_token())):
        raise HTTPException(401, "admin token required")
    db.kv_del(_TOTP_KEY)
    db.admin_sessions_clear()
    _PENDING["secret"] = ""
    return {"ok": True, "enrolled": False}


@app.get("/admin/api/keys", include_in_schema=False, dependencies=[Depends(admin_auth)])
def admin_keys():
    return {"object": "list", "data": db.list_keys()}


@app.post("/admin/api/keys", include_in_schema=False, dependencies=[Depends(admin_auth)])
def admin_key_create(body: KeyCreate):
    return db.create_key(body.name, body.quota_credits)


@app.delete("/admin/api/keys/{kid}", include_in_schema=False, dependencies=[Depends(admin_auth)])
def admin_key_delete(kid: str):
    if not db.revoke_key(kid):
        raise HTTPException(404, "key not found")
    return {"id": kid, "active": False}


@app.get("/admin/api/jobs", include_in_schema=False, dependencies=[Depends(admin_auth)])
def admin_jobs(limit: int = Query(default=20, ge=1, le=100)):
    return {"object": "list", "data": db.recent_jobs(limit)}


@app.get("/admin/api/pool", include_in_schema=False, dependencies=[Depends(admin_auth)])
def admin_pool():
    st = pool.stats() if pool is not None else {"error": "control-plane mode (no pool on this host)"}
    st["jobs"] = db.counts()
    return st


@app.get("/admin/api/workers", include_in_schema=False, dependencies=[Depends(admin_auth)])
def admin_workers():
    """Render workers that sent a heartbeat in the last 90 seconds."""
    now = time.time()
    out = []
    for k, v in db.kv_items("worker_hb:").items():
        try:
            d = json.loads(v)
        except Exception:
            continue
        age = max(0.0, now - float(d.get("ts") or 0))
        out.append({"wid": k.split(":", 1)[1], "age_s": round(age, 1),
                    "online": age < 90, "info": d.get("info") or {}})
    out.sort(key=lambda x: x["age_s"])
    return {"object": "list", "now": now, "data": out}
