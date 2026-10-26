# -*- coding: utf-8 -*-
"""app.py - OpenAI-style video API backed by a Pippit account pool.

Run:
  python pool_api/app.py
  (env: WZ_PORT=8788 WZ_MAX_WORKERS=2 WZ_ADMIN_TOKEN=...)

Public:
  GET  /v1/models
  POST /v1/videos
  GET  /v1/videos/{id}
  GET  /v1/videos/{id}/content
  GET  /v1/videos
  POST /v1/videos/{id}/cancel

Admin (X-Admin-Token):
  POST   /admin/keys
  GET    /admin/keys
  DELETE /admin/keys/{id}
  POST   /admin/accounts/import
  GET    /admin/accounts
  POST   /admin/accounts/refresh
  GET    /admin/jobs
"""
import json, os, secrets, sys, time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from pydantic import BaseModel, Field

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, r"<PROJECT_ROOT>\work")

import db
import panel
from pippit import PippitAccount, estimate_cost
from worker import start_workers

DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
TOKEN_FILE = os.path.join(DATA, "admin_token.txt")


def _load_admin_token():
    tok = os.environ.get("WZ_ADMIN_TOKEN")
    if tok:
        return tok
    try:
        tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if tok:
            return tok
    except Exception:
        pass
    tok = "wz-admin-" + secrets.token_urlsafe(24)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok)
    return tok


ADMIN_TOKEN = _load_admin_token()
PER_KEY_CONCURRENCY = int(os.environ.get("WZ_PER_KEY_CONCURRENCY", "2"))
MAX_SECONDS = int(os.environ.get("WZ_MAX_SECONDS", "30"))
DEFAULT_MODEL = "pippit-smart-1080p-30s"
ALIASES = {
    "pippit-smart-1080p-30s": ("1080p", "9:16"),
    "seedance-2.0-smart-1080p-30s": ("1080p", "9:16"),
    "pippit-smart-1080p": ("1080p", "9:16"),
    "pippit-smart-720p-30s": ("720p", "9:16"),
    "smart": ("1080p", "9:16"),
    "auto": ("1080p", "9:16"),
}
MODEL_LIST = [
    {"id": "pippit-smart-1080p-30s", "object": "model", "created": int(time.time()), "owned_by": "wz-pool",
     "metadata": {"resolution": "1080p", "size": "1080x1920", "seconds": 30, "credit_cost": 120,
                  "engine": "Pippit Smart/Auto (Seedance 2.0 lane)", "watermark": False}},
    {"id": "pippit-smart-720p-30s", "object": "model", "created": int(time.time()), "owned_by": "wz-pool",
     "metadata": {"resolution": "720p", "size": "720x1280", "seconds": 30, "credit_cost": 120,
                  "engine": "Pippit Smart/Auto (Seedance 2.0 lane)", "watermark": False}},
]

DESCRIPTION = """
Pippit 号池的 OpenAI 风格视频生成接口。一个 Key 即可调用整个号池，服务端负责选号、排队、生成、落盘与自动清理。

**能力**
- 文生视频 / 图生视频 / 视频参考 / 音频参考（音频必须搭配图片或视频）
- 默认 30 秒、1080x1920、30fps、H.264、无水印
- 任务异步执行，支持轮询、取消、HEAD 探测与 Range 下载
- 每个 Key 默认并发 2；号池按积分自动调度

**鉴权**
- `/v1/*`：`Authorization: Bearer <api_key>`
- `/admin/*`：`X-Admin-Token: <admin_token>`
- `/master/api/*`：面板登录后的 HttpOnly Cookie

**典型流程**
1. `POST /v1/videos` 创建任务，拿到 `id`
2. `GET /v1/videos/{id}` 轮询，直到 `status=completed`
3. `GET /v1/videos/{id}/content` 下载 mp4（支持 Range）
"""

OPENAPI_TAGS = [
    {"name": "公开 API", "description": "给业务方调用的 OpenAI 风格接口，使用 Bearer Key 鉴权。"},
    {"name": "管理 API", "description": "号池运维接口，使用 X-Admin-Token 鉴权。"},
    {"name": "面板 API", "description": "总台面板内部接口，使用登录 Cookie 鉴权。"},
    {"name": "系统", "description": "健康检查与服务信息，无需鉴权。"},
]

app = FastAPI(
    title="Pippit Pool API",
    version="1.1.0",
    description=DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
app.include_router(panel.router)


@app.on_event("startup")
def _startup():
    db.init()
    start_workers(int(os.environ.get("WZ_MAX_WORKERS", "2")))
    panel.start_cleanup(int(os.environ.get("WZ_SWEEP_INTERVAL", "1800")))


# ---------------- auth ----------------
def auth_key(authorization: Optional[str] = Header(None, description="Bearer API Key，格式：Bearer sk-pp-REPLACE_ME")):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer key")
    row = db.verify_key(authorization.split(" ", 1)[1].strip())
    if not row:
        raise HTTPException(401, "invalid or revoked api key")
    return row


def auth_admin(
    x_admin_token: Optional[str] = Header(None, description="管理令牌，值见服务器 data/admin_token.txt"),
    authorization: Optional[str] = Header(None, description="也可以用 Bearer <admin_token> 代替 X-Admin-Token"),
):
    tok = x_admin_token
    if not tok and authorization and authorization.lower().startswith("bearer "):
        tok = authorization.split(" ", 1)[1].strip()
    if tok != ADMIN_TOKEN:
        raise HTTPException(401, "admin token required")
    return True


# ---------------- models ----------------
def _err(msg, typ="invalid_request_error", code=None, status=400):
    return JSONResponse(status_code=status,
                        content={"error": {"message": msg, "type": typ, "code": code}})


class VideoRequest(BaseModel):
    model: str = Field(
        DEFAULT_MODEL,
        description="模型 ID 或别名。可选 pippit-smart-1080p-30s（默认）、pippit-smart-720p-30s、"
                    "pippit-smart-1080p、seedance-2.0-smart-1080p-30s、smart、auto。",
        examples=["pippit-smart-1080p-30s"],
    )
    prompt: str = Field(
        ..., min_length=1,
        description="视频提示词。建议写明主体、动作、镜头、光线与风格；纯文本模式可生成 30 秒。",
        examples=["a cinematic sunrise over a misty lake, slow push-in, 6 shots, no text"],
    )
    seconds: Optional[int] = Field(
        None,
        description="视频时长（秒），5–30。与 duration 二选一；都不传默认 30。带参考素材时平台通常只接受 10–20 秒。",
        examples=[30],
    )
    duration: Optional[int] = Field(
        None,
        description="seconds 的别名，兼容部分 OpenAI SDK。",
        examples=[30],
    )
    size: str = Field(
        "1080x1920",
        description="输出尺寸。支持 1080x1920、1920x1080、1080x1080、720x1280、1280x720、720x720；会据此推导 ratio。",
        examples=["1080x1920"],
    )
    resolution: Optional[str] = Field(
        None,
        description="清晰度：720p 或 1080p。不传则按模型默认值。",
        examples=["1080p"],
    )
    ratio: Optional[str] = Field(
        None,
        description="画面比例：9:16、16:9、1:1。size 能识别时以 size 为准。",
        examples=["9:16"],
    )
    n: int = Field(
        1,
        description="生成条数。当前只支持 1。",
        examples=[1],
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="调用方自定义元数据，仅随任务保存，不影响生成。",
        examples=[{"client": "demo", "trace_id": "abc-123"}],
    )
    reference_image_url: Optional[str] = Field(
        None,
        description="参考图：http(s) URL 或服务器本地绝对路径。用于图生视频 / 首帧参考。",
        examples=["https://example.com/ref.jpg"],
    )
    reference_video_url: Optional[str] = Field(
        None,
        description="参考视频：http(s) URL 或服务器本地绝对路径。用于动作 / 风格参考。",
        examples=["https://example.com/ref.mp4"],
    )
    reference_audio_url: Optional[str] = Field(
        None,
        description="参考音频：http(s) URL 或服务器本地绝对路径。必须同时提供参考图或参考视频，否则 400。",
        examples=["https://example.com/ref.mp3"],
    )
    reference_image: Optional[str] = Field(
        None,
        description="reference_image_url 的简写别名。",
        examples=["https://example.com/ref.jpg"],
    )
    reference_video: Optional[str] = Field(
        None,
        description="reference_video_url 的简写别名。",
        examples=["https://example.com/ref.mp4"],
    )
    reference_audio: Optional[str] = Field(
        None,
        description="reference_audio_url 的简写别名。",
        examples=["https://example.com/ref.mp3"],
    )


def _normalize(req: VideoRequest):
    model = (req.model or DEFAULT_MODEL).strip()
    base_res, base_ratio = ALIASES.get(model, ("1080p", "9:16"))
    seconds = req.seconds if req.seconds is not None else (req.duration if req.duration is not None else 30)
    seconds = int(seconds)
    if not (5 <= seconds <= MAX_SECONDS):
        raise HTTPException(400, "seconds must be 5..%d" % MAX_SECONDS)
    res = (req.resolution or base_res).lower()
    if res not in ("720p", "1080p"):
        raise HTTPException(400, "resolution must be 720p or 1080p")
    ratio = req.ratio or base_ratio
    size = (req.size or "").lower()
    if size in ("1080x1920", "720x1280"):
        ratio = "9:16"
    elif size in ("1920x1080", "1280x720"):
        ratio = "16:9"
    elif size in ("1080x1080", "720x720"):
        ratio = "1:1"
    if ratio not in ("9:16", "16:9", "1:1"):
        raise HTTPException(400, "ratio must be 9:16, 16:9 or 1:1")
    if req.n != 1:
        raise HTTPException(400, "only n=1 is supported")
    return model, seconds, res, ratio


def _size_for(res, ratio):
    if ratio == "9:16":
        return "1080x1920" if res == "1080p" else "720x1280"
    if ratio == "16:9":
        return "1920x1080" if res == "1080p" else "1280x720"
    return "1080x1080" if res == "1080p" else "720x720"


def _job_json(j, host):
    status = j.get("status") or "queued"
    out = {
        "id": j["id"], "object": "video", "created_at": int(j.get("created_at") or 0),
        "status": {"running": "in_progress"}.get(status, status),
        "model": j.get("model"), "progress": int(j.get("progress") or 0),
        "seconds": int(j.get("seconds") or 30),
        "size": _size_for(j.get("resolution") or "1080p", j.get("ratio") or "9:16"),
        "prompt": j.get("prompt"),
    }
    refs = {k: j.get("ref_" + k) for k in ("image", "video", "audio") if j.get("ref_" + k)}
    if refs:
        out["references"] = refs
    if status == "completed":
        out["output_url"] = host + "/v1/videos/%s/content" % j["id"]
        out["width"] = j.get("width"); out["height"] = j.get("height")
        out["duration"] = j.get("duration"); out["bytes"] = j.get("bytes")
        out["asset_id"] = j.get("asset_id")
        out["completed_at"] = int(j.get("completed_at") or 0)
    if status == "failed":
        out["error"] = {"message": (j.get("error") or "generation failed")[:1000], "type": "generation_error"}
    return out


@app.get("/v1/models", tags=["公开 API"], summary="模型列表",
         description="返回当前可用的模型、输出规格与积分成本。需要 Bearer Key。",
         responses={401: {"description": "缺少或无效的 API Key"}})
def models(_key=Depends(auth_key)):
    return {"object": "list", "data": MODEL_LIST}


def _fix_mojibake(s):
    """Recover UTF-8 text that a client sent as latin-1 (PowerShell 5.1 default)."""
    if not isinstance(s, str) or not s:
        return s
    if not any("\u0080" <= ch <= "\u00ff" for ch in s):
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except Exception:
        return s
    if any("\u4e00" <= ch <= "\u9fff" for ch in fixed):
        return fixed
    return s


@app.post("/v1/videos", tags=["公开 API"], summary="创建视频任务",
         description="""创建一条异步视频任务，立即返回任务对象（status=queued）。
服务端按账号积分自动选号；单条 30 秒约消耗 120 积分。
带 reference_image/video/audio 时平台通常限制 10–20 秒；30 秒请使用纯文本提示词。
每个 Key 默认最多 2 个进行中的任务。""",
         response_description="任务对象，含 id、status、progress、seconds、size",
         responses={
             400: {"description": "参数错误（时长、比例、参考素材组合不合法等）"},
             401: {"description": "缺少或无效的 API Key"},
             429: {"description": "Key 并发或额度超限"},
             503: {"description": "号池没有账号能负担本次积分"},
         })
async def create_video(req: VideoRequest, request: Request, key=Depends(auth_key)):
    req.prompt = _fix_mojibake(req.prompt)
    model, seconds, res, ratio = _normalize(req)
    running = db.running_for_key(key["id"])
    if running >= PER_KEY_CONCURRENCY:
        return _err("per-key concurrency limit reached (%d)" % PER_KEY_CONCURRENCY, "rate_limit_error", status=429)
    if key.get("quota") and (int(key.get("used") or 0) + running) >= int(key["quota"]):
        return _err("key quota exhausted", "insufficient_quota", status=429)
    cost = estimate_cost(seconds)
    cap = db.queryone("SELECT COUNT(*) n FROM accounts "
                      "WHERE status IN ('ready','busy') AND credit>=?", (cost,)) or {}
    if int(cap.get("n") or 0) == 0:
        mx = db.queryone("SELECT COALESCE(MAX(credit),0) mx FROM accounts") or {}
        return _err("no account can afford this job: need %d credits, pool max is %d. "
                    "Redeem daily rewards or add accounts." % (cost, int(mx.get("mx") or 0)),
                    "insufficient_quota", status=503)
    refs = {
        "image": (req.reference_image_url or req.reference_image or "").strip(),
        "video": (req.reference_video_url or req.reference_video or "").strip(),
        "audio": (req.reference_audio_url or req.reference_audio or "").strip(),
    }
    refs = {k: v for k, v in refs.items() if v}
    if refs.get("audio") and not (refs.get("image") or refs.get("video")):
        return _err("reference_audio_url must be paired with reference_image_url or reference_video_url",
                    status=400)
    jid = db.create_job(key["id"], req.prompt, model, seconds, res, ratio, cost, refs=refs)
    db.bump_key_usage(key["id"])
    db.add_event(jid, "created model=%s seconds=%d res=%s ratio=%s cost=%d refs=%s" %
                 (model, seconds, res, ratio, cost, ",".join(sorted(refs)) or "-"))
    j = db.get_job(jid)
    return _job_json(j, str(request.base_url).rstrip("/"))


@app.post("/v1/video/generations", tags=["公开 API"], summary="创建视频任务（OpenAI 别名）",
         description="与 POST /v1/videos 完全等价，兼容部分 OpenAI 风格 SDK。",
         responses={
             400: {"description": "参数错误"},
             401: {"description": "缺少或无效的 API Key"},
             429: {"description": "Key 并发或额度超限"},
             503: {"description": "号池没有账号能负担本次积分"},
         })
async def create_video_alias(req: VideoRequest, request: Request, key=Depends(auth_key)):
    return await create_video(req, request, key)


@app.get("/v1/videos", tags=["公开 API"], summary="查询任务列表",
         description="按创建时间倒序返回当前 Key 的任务，最多 200 条。",
         responses={401: {"description": "缺少或无效的 API Key"}})
def list_videos(limit: int = Query(50, ge=1, le=200, description="返回条数，1–200"), key=Depends(auth_key)):
    rows = db.list_jobs(key["id"], min(int(limit), 200))
    return {"object": "list", "data": [_job_json(j, "") for j in rows]}


@app.get("/v1/videos/{vid}", tags=["公开 API"], summary="查询单个任务",
         description="轮询任务状态与进度。status=completed 后可下载；failed 时带 error 字段。",
         responses={
             401: {"description": "缺少或无效的 API Key"},
             404: {"description": "任务不存在，或不属于当前 Key"},
         })
def get_video(request: Request, vid: str = Path(..., description="任务 ID，即 POST /v1/videos 返回的 id"), key=Depends(auth_key)):
    j = db.get_job(vid)
    if not j or j.get("key_id") != key["id"]:
        raise HTTPException(404, "video not found")
    return _job_json(j, str(request.base_url).rstrip("/"))


def _content_path(vid, key):
    j = db.get_job(vid)
    if not j or j.get("key_id") != key["id"]:
        raise HTTPException(404, "video not found")
    if j.get("status") != "completed" or not j.get("video_path"):
        raise HTTPException(409, "video not ready (status=%s)" % j.get("status"))
    p = j["video_path"]
    if not os.path.exists(p):
        raise HTTPException(410, "video file missing on server")
    return p


@app.get("/v1/videos/{vid}/content", tags=["公开 API"], summary="下载视频（支持 Range）",
         description="返回 mp4 文件。支持 HTTP Range（206 分段下载）。本地文件被自动清理后返回 410。",
         response_description="video/mp4 二进制流",
         responses={
             200: {"description": "完整 mp4"},
             206: {"description": "Range 分段内容"},
             401: {"description": "缺少或无效的 API Key"},
             404: {"description": "任务不存在，或不属于当前 Key"},
             409: {"description": "任务尚未完成"},
             410: {"description": "本地文件已被自动清理"},
         })
def get_content(vid: str = Path(..., description="任务 ID"), key=Depends(auth_key)):
    p = _content_path(vid, key)
    return FileResponse(p, media_type="video/mp4", filename=os.path.basename(p))


@app.head("/v1/videos/{vid}/content", tags=["公开 API"], summary="探测视频（HEAD）",
         description="只返回响应头，包含 Content-Length 与 Accept-Ranges: bytes，可用于下载前探测大小。",
         responses={
             200: {"description": "文件可下载"},
             401: {"description": "缺少或无效的 API Key"},
             404: {"description": "任务不存在，或不属于当前 Key"},
             409: {"description": "任务尚未完成"},
             410: {"description": "本地文件已被自动清理"},
         })
def head_content(vid: str = Path(..., description="任务 ID"), key=Depends(auth_key)):
    p = _content_path(vid, key)
    return Response(status_code=200, media_type="video/mp4",
                    headers={"Content-Length": str(os.path.getsize(p)),
                             "Accept-Ranges": "bytes"})


@app.post("/v1/videos/{vid}/cancel", tags=["公开 API"], summary="取消排队中的任务",
         description="只能取消 status=queued 的任务；已经开始的生成任务返回 409。",
         responses={
             401: {"description": "缺少或无效的 API Key"},
             404: {"description": "任务不存在，或不属于当前 Key"},
             409: {"description": "任务已经开始，不能取消"},
         })
def cancel_video(vid: str = Path(..., description="任务 ID"), key=Depends(auth_key)):
    j = db.get_job(vid)
    if not j or j.get("key_id") != key["id"]:
        raise HTTPException(404, "video not found")
    if j.get("status") != "queued":
        raise HTTPException(409, "only queued jobs can be canceled")
    db.update_job(vid, status="canceled", stage="canceled", completed_at=time.time())
    return _job_json(db.get_job(vid), "")


# ---------------- admin ----------------
class KeyReq(BaseModel):
    name: str = Field("default", description="Key 名称，便于区分调用方", examples=["client-a"])
    quota: int = Field(0, description="累计任务额度；0 表示不限", examples=[0])
    expires_in_days: Optional[int] = Field(None, description="有效天数；null 表示永不过期", examples=[30])


@app.post("/admin/keys", tags=["管理 API"], summary="创建 API Key",
         description="创建一个新的 Bearer Key。明文只在本次响应返回，请立即保存。",
         responses={401: {"description": "管理令牌错误"}})
def create_key(req: KeyReq, _=Depends(auth_admin)):
    kid, raw = db.create_key(req.name, req.quota, req.expires_in_days)
    return {"id": kid, "key": raw, "name": req.name, "quota": req.quota,
            "expires_in_days": req.expires_in_days,
            "note": "store this key now; it is not shown again"}


@app.get("/admin/keys", tags=["管理 API"], summary="Key 列表",
         description="返回全部 Key 的元数据（不含明文）。",
         responses={401: {"description": "管理令牌错误"}})
def list_keys(_=Depends(auth_admin)):
    return {"object": "list", "data": db.list_keys()}


@app.delete("/admin/keys/{kid}", tags=["管理 API"], summary="撤销 Key",
         description="将 Key 标记为失效，历史任务保留。",
         responses={401: {"description": "管理令牌错误"}})
def delete_key(kid: str = Path(..., description="Key ID"), _=Depends(auth_admin)):
    db.revoke_key(kid)
    return {"id": kid, "active": False}


class ImportReq(BaseModel):
    accounts: Optional[List[Dict[str, Any]]] = Field(
        None, description="账号对象数组。每个对象至少包含 uid 与 cookies；可选 ports、tag、workspace_id、space_id、credit。",
        examples=[[{"uid": "123456", "cookies": {"sessionid": "..."}, "ports": [22030, 22031, 22032]}]])
    paths: Optional[List[str]] = Field(
        None, description="服务器本地 JSON 文件路径数组。文件内容可以是账号对象、账号数组或指向账号文件的路径。",
        examples=[["/opt/pippit-pool/data/accounts.json"]])
    bootstrap: bool = Field(True, description="是否立即登录平台并拉取 workspace/space/积分", examples=[True])
    min_credit: int = Field(0, description="只保留积分不低于该值的导入结果", examples=[0])


def _load_accounts(req: ImportReq):
    out = []
    for a in req.accounts or []:
        out.append(a)
    for p in req.paths or []:
        try:
            obj = json.load(open(p, encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(obj, list):
            for x in obj:
                if isinstance(x, dict):
                    out.append(x)
                elif isinstance(x, str) and os.path.exists(x):
                    try:
                        y = json.load(open(x, encoding="utf-8-sig"))
                        if isinstance(y, dict):
                            out.append(y)
                    except Exception:
                        pass
        elif isinstance(obj, dict):
            out.append(obj)
    return out


def _import_one(a, do_bootstrap=True):
    uid = str(a.get("uid") or "")
    ck = a.get("cookies") or {}
    if not uid or not ck:
        return {"uid": uid, "ok": False, "err": "missing uid/cookies"}
    ports = a.get("ports") or ([a["port"]] if a.get("port") else []) or [22030, 22031, 22032]
    ports = [int(x) for x in ports]
    ws, sp, credit, status, err = a.get("workspace_id"), a.get("space_id"), int(a.get("credit") or 0), "ready", None
    if do_bootstrap:
        try:
            c = PippitAccount(ck, uid=uid, ports=ports, tag=a.get("tag"))
            ws, sp = c.bootstrap()
            credit = c.credit()["total"]
            if credit <= 0:
                status = "ready"
        except Exception as e:
            status, err = "dead", repr(e)[:300]
    db.upsert_account(uid, a.get("tag") or "", ck, ports, ws, sp, credit, status, err)
    return {"uid": uid, "ok": err is None, "credit": credit, "status": status, "err": err}


@app.post("/admin/accounts/import", tags=["管理 API"], summary="导入账号",
         description="导入一个或多个号池账号；bootstrap=true 时会立即登录平台校验并刷新积分。",
         responses={
             400: {"description": "没有解析到任何账号"},
             401: {"description": "管理令牌错误"},
         })
def import_accounts(req: ImportReq, _=Depends(auth_admin)):
    rows = _load_accounts(req)
    if not rows:
        raise HTTPException(400, "no accounts")
    res = []
    if req.bootstrap:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(lambda a: _import_one(a, True), rows):
                res.append(r)
    else:
        res = [_import_one(a, False) for a in rows]
    res = [r for r in res if (r.get("credit") or 0) >= req.min_credit or r.get("status") == "dead"]
    return {"imported": len(res), "ok": sum(1 for r in res if r.get("ok")), "accounts": res}


@app.get("/admin/accounts", tags=["管理 API"], summary="账号列表",
         description="返回账号状态、积分、最近错误与任务计数；cookies 只显示数量。",
         responses={401: {"description": "管理令牌错误"}})
def admin_accounts(_=Depends(auth_admin)):
    rows = db.list_accounts()
    out = []
    for r in rows:
        out.append({k: r.get(k) for k in ("uid", "tag", "credit", "status", "cooldown_until",
                                          "last_error", "jobs_done", "jobs_failed", "last_used",
                                          "workspace_id", "space_id", "ports")}
                   | {"cookies": "%d cookies" % len(r.get("cookies") or {})})
    return {"object": "list", "count": len(out), "data": out}


class RefreshReq(BaseModel):
    uids: Optional[List[str]] = Field(None, description="只刷新指定 uid；不传则刷新全部账号", examples=[["123456"]])
    claim: bool = Field(False, description="刷新前先领取每日任务奖励（claim_all）", examples=[False])


@app.post("/admin/accounts/refresh", tags=["管理 API"], summary="刷新账号积分",
         description="重新登录平台读取积分；claim=true 时先领取每日任务奖励。可指定 uids，默认全部。",
         responses={401: {"description": "管理令牌错误"}})
def refresh_accounts(req: RefreshReq, _=Depends(auth_admin)):
    rows = db.list_accounts()
    if req.uids:
        want = set(str(x) for x in req.uids)
        rows = [r for r in rows if str(r["uid"]) in want]
    out = []

    def one(r):
        try:
            c = PippitAccount(r["cookies"], uid=r["uid"], ports=r["ports"] or [22030, 22031, 22032], tag=r.get("tag"))
            if req.claim:
                c.claim_all(stop_at=340)
            credit = c.credit()["total"]
            db.set_account_credit(r["uid"], credit)
            return {"uid": r["uid"], "credit": credit}
        except Exception as e:
            return {"uid": r["uid"], "err": repr(e)[:200]}

    with ThreadPoolExecutor(max_workers=4) as ex:
        for x in ex.map(one, rows):
            out.append(x)
    return {"refreshed": len(out), "data": out}


@app.get("/admin/jobs", tags=["管理 API"], summary="全部任务",
         description="返回全号池任务，按创建时间倒序，最多 500 条。",
         responses={401: {"description": "管理令牌错误"}})
def admin_jobs(limit: int = Query(100, ge=1, le=500, description="返回条数，1–500"), _=Depends(auth_admin)):
    return {"object": "list", "data": db.list_jobs(None, min(int(limit), 500))}


@app.get("/healthz", tags=["系统"], summary="健康检查",
         description="返回服务存活状态、服务器时间与 admin token 文件位置。无需鉴权。")
def health():
    return {"ok": True, "time": int(time.time()), "admin_token_file": TOKEN_FILE}


@app.get("/", tags=["系统"], summary="服务信息",
         description="返回服务入口、文档地址与面板地址。无需鉴权。")
def root():
    return {"service": "Pippit Pool API", "models": "/v1/models", "docs": "/docs",
            "master": "/master", "admin_token_file": TOKEN_FILE}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("WZ_HOST", "0.0.0.0"),
                port=int(os.environ.get("WZ_PORT", "8788")), log_level="info")