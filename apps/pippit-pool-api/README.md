# Pippit Pool API

> 把 Pippit / Dreamina 账号池封装成 OpenAI 风格视频生成 API。
> 交付契约：`1080x1920` / `900 帧` / `30.000s` / 无水印（CLEAN）。

## 功能

- OpenAI 风格 `POST /v1/videos`、`GET /v1/videos/{id}`、`GET /v1/videos/{id}/content`
- 多 API Key、配额、过期时间、按 Key 并发限制
- 账号池自动选号、积分检查、UID 粒度锁、失败换号
- 任务队列、进度、取消、Range 下载、结果元数据
- 参考图 / 参考视频 / 参考音频上传
- Google Authenticator 管理后台与 OpenAPI 文档
- 质量门：分辨率、帧数、时长、水印四项校验

## 目录

```text
pippit-pool-api/
├─ app.py              FastAPI 入口、任务与下载 API
├─ db.py               SQLite schema / Key / Jobs / Accounts
├─ panel.py            管理后台与 TOTP 会话
├─ panel.html          管理面板
├─ pippit.py           Pippit 协议客户端
├─ refs.py             参考素材上传
├─ worker.py           后台 worker
├─ import_accounts.py  账号导入
├─ API.md              全量接口文档
├─ openapi.json        机器可读 OpenAPI
└─ PROTOCOL.md         协议要点
```

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export WZ_HOST=127.0.0.1
export WZ_PORT=8788
export WZ_ADMIN_TOKEN='<CHANGE_ME>'
python app.py
```

管理令牌也可由服务首次启动时写入 `data/admin_token.txt`。生产环境建议只监听 `127.0.0.1`，由 nginx 终结 TLS。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WZ_HOST` | `0.0.0.0` | 监听地址，生产建议 `127.0.0.1` |
| `WZ_PORT` | `8788` | HTTP 端口 |
| `WZ_MAX_WORKERS` | `2` | 后台 worker 数 |
| `WZ_PER_KEY_CONCURRENCY` | `2` | 单 Key 并发任务数 |
| `WZ_MAX_SECONDS` | `30` | 最大时长 |
| `WZ_AUTO_CLAIM` | `0` | 是否自动领取任务积分 |
| `WZ_ADMIN_TOKEN` | 空 | 管理令牌；空则自动生成 |

## 创建 Key

```bash
curl -X POST http://127.0.0.1:8788/admin/keys \
  -H "X-Admin-Token: <CHANGE_ME>" \
  -H "Content-Type: application/json" \
  -d '{"name":"demo","quota":240}'
```

返回的 `sk-pp-...` 明文只显示一次。

## 提交 30 秒任务

```bash
curl -X POST http://127.0.0.1:8788/v1/videos \
  -H "Authorization: Bearer sk-pp-REPLACE_ME" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"pippit-smart-1080p-30s",
    "prompt":"Vertical 9:16 30-second coffee commercial, six 5-second shots, no text, no logos.",
    "seconds":30,
    "size":"1080x1920"
  }'
```

轮询：

```bash
curl -H "Authorization: Bearer sk-pp-REPLACE_ME" \
  http://127.0.0.1:8788/v1/videos/video_xxx
```

下载：

```bash
curl -H "Authorization: Bearer sk-pp-REPLACE_ME" \
  -o out.mp4 \
  http://127.0.0.1:8788/v1/videos/video_xxx/content
```

## 主要端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/healthz` | - | 健康检查 |
| GET | `/v1/models` | - | 模型列表 |
| POST | `/v1/videos` | Key | 创建任务 |
| GET | `/v1/videos/{id}` | Key | 查询任务 |
| GET | `/v1/videos/{id}/content` | Key | 下载 mp4 |
| POST | `/v1/videos/{id}/cancel` | Key | 取消排队任务 |
| POST | `/admin/keys` | Admin | 创建 Key |
| GET | `/admin/accounts` | Admin | 账号池状态 |
| POST | `/admin/accounts/import` | Admin | 导入账号 |
| GET | `/master` | TOTP | 管理面板 |

完整字段见 [`API.md`](API.md) 与 [`openapi.json`](openapi.json)。

## 积分与产能

| 项 | 数值 |
|---|---|
| 30s / 1080p | 约 120~126 分 |
| 15s | 约 60 分 |
| 340 分账号 | 约 2 条 30s + 100 分余量 |
| 失败处理 | 余额不足换号；网络错误重试；质量失败重跑 |

## 质量门

- 分辨率：`1080x1920`（或请求指定）
- 帧数：`900`（30fps × 30s）
- 时长：`30.000s`，容差 ±0.35s
- 水印：`CLEAN`

不达标不会标记完成，会进入重试/人工复核。

## 安全

- API Key 只存 SHA-256；明文只在创建时返回。
- 管理端 TOTP + 登录限流；会话存 SQLite。
- 账号 UID 锁跨进程互斥。
- 不记录 Cookie、完整 Key、手机号。
- 生产只监听 `127.0.0.1`，由反向代理终结 TLS。

## 测试

```bash
python -m compileall .
curl -s http://127.0.0.1:8788/healthz
```

## 免责声明

仅用于自有账号、已授权账号或合规测试环境。请遵守目标平台服务条款与当地法律。
