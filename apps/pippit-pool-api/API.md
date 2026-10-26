# Pippit Pool API 接口文档

> 全量接口文档，由 FastAPI OpenAPI 自动生成并人工补充说明。机器可读版本：[openapi.json](openapi.json)

## 基本信息

- **Base URL**：`http://127.0.0.1:8788`
- **API 版本**：`1.1.0`
- **OpenAPI**：`http://127.0.0.1:8788/openapi.json`
- **Swagger UI**：`http://127.0.0.1:8788/docs`
- **ReDoc**：`http://127.0.0.1:8788/redoc`
- **总台面板**：`http://127.0.0.1:8788/master`
- **默认模型**：`pippit-smart-1080p-30s`（1080x1920 / 30s / 30fps / H.264 / 无水印）
- **默认并发**：每个 Key 2 个进行中的任务
- **积分成本**：30 秒约 120 积分；15 秒约 60 积分

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

## 鉴权

| 范围 | 方式 | 说明 |
|---|---|---|
| `/v1/*` | `Authorization: Bearer sk-pp-REPLACE_ME` | 在面板「密钥」页或 `POST /admin/keys` 创建 |
| `/admin/*` | `X-Admin-Token: <token>` | 值见服务器 `/opt/pippit-pool/data/admin_token.txt` |
| `/master/api/*` | `Cookie: pp_master=<session>` | 面板 6 位 TOTP 登录后自动下发，7 天有效 |
| `/healthz`、`/` | 无 | 健康检查与服务信息 |

## 模型

| 模型 ID | 分辨率 | 尺寸 | 时长 | 说明 |
|---|---|---|---|---|
| `pippit-smart-1080p-30s` | 1080p | 1080x1920 | 30s | Pippit Smart/Auto (Seedance 2.0 lane) |
| `pippit-smart-720p-30s` | 720p | 720x1280 | 30s | Pippit Smart/Auto (Seedance 2.0 lane) |

别名：`smart`、`auto`、`pippit-smart-1080p`、`seedance-2.0-smart-1080p-30s`。

## 参数与边界

- `seconds` / `duration`：5–30；都不传默认 30。
- `n`：当前只支持 1。
- `resolution`：`720p` 或 `1080p`；`ratio`：`9:16`、`16:9`、`1:1`。
- `size`：`1080x1920`、`1920x1080`、`1080x1080`、`720x1280`、`1280x720`、`720x720`。
- 参考素材：`reference_image_url` / `reference_video_url` / `reference_audio_url`，支持 http(s) URL 或服务器本地绝对路径。
- 音频参考必须同时提供图片或视频参考。
- 带参考素材时平台通常只接受 10–20 秒；**30 秒必须使用纯文本提示词**。
- 无账号能负担本次积分时返回 `503`，不会创建任务。

## 接口总览

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| GET | `/master` | 总台面板页面 | 无 |
| GET | `/master/api/state` | 绑定 / 登录状态 | Cookie |
| POST | `/master/api/enroll/start` | 开始绑定 TOTP | Cookie |
| POST | `/master/api/enroll/confirm` | 确认绑定 TOTP | Cookie |
| POST | `/master/api/login` | 验证码登录 | Cookie |
| POST | `/master/api/logout` | 退出登录 | Cookie |
| GET | `/master/api/overview` | 看板总览 | Cookie |
| GET | `/master/api/jobs` | 任务列表 | Cookie |
| GET | `/master/api/jobs/{jid}` | 任务详情 + 事件 | Cookie |
| GET | `/master/api/jobs/{jid}/video` | 播放本地成片 | Cookie |
| GET | `/master/api/keys` | Key 列表 | Cookie |
| POST | `/master/api/keys` | 创建 Key | Cookie |
| DELETE | `/master/api/keys/{kid}` | 撤销 Key | Cookie |
| GET | `/master/api/accounts` | 账号列表 | Cookie |
| GET | `/master/api/settings` | 读取清理设置 | Cookie |
| POST | `/master/api/settings` | 保存清理设置 | Cookie |
| POST | `/master/api/sweep` | 执行 / 预览清理 | Cookie |
| GET | `/v1/models` | 模型列表 | Bearer |
| POST | `/v1/videos` | 创建视频任务 | Bearer |
| GET | `/v1/videos` | 查询任务列表 | Bearer |
| POST | `/v1/video/generations` | 创建视频任务（OpenAI 别名） | Bearer |
| GET | `/v1/videos/{vid}` | 查询单个任务 | Bearer |
| GET | `/v1/videos/{vid}/content` | 下载视频（支持 Range） | Bearer |
| HEAD | `/v1/videos/{vid}/content` | 探测视频（HEAD） | Bearer |
| POST | `/v1/videos/{vid}/cancel` | 取消排队中的任务 | Bearer |
| POST | `/admin/keys` | 创建 API Key | Admin Token |
| GET | `/admin/keys` | Key 列表 | Admin Token |
| DELETE | `/admin/keys/{kid}` | 撤销 Key | Admin Token |
| POST | `/admin/accounts/import` | 导入账号 | Admin Token |
| GET | `/admin/accounts` | 账号列表 | Admin Token |
| POST | `/admin/accounts/refresh` | 刷新账号积分 | Admin Token |
| GET | `/admin/jobs` | 全部任务 | Admin Token |
| GET | `/healthz` | 健康检查 | 无 |
| GET | `/` | 服务信息 | 无 |
| GET | `/openapi.json` | OpenAPI 规范 | 无 |
| GET | `/docs` | Swagger UI | 无 |
| GET | `/redoc` | ReDoc | 无 |

## 接口详情

### `GET /master`

**总台面板页面**

返回总台单页应用。首次访问扫码绑定 TOTP，之后用 6 位验证码登录。

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |

```bash
curl -X GET 'http://127.0.0.1:8788/master'
```

### `GET /master/api/state`

**绑定 / 登录状态**

返回是否已完成 TOTP 绑定，以及当前请求是否携带有效登录 Cookie。

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | enrolled + authed |

```bash
curl -X GET 'http://127.0.0.1:8788/master/api/state' \
  -H 'Cookie: pp_master=<session>'
```

### `POST /master/api/enroll/start`

**开始绑定 TOTP**

生成新的 TOTP 密钥，返回 otpauth URI、PNG 二维码与 SVG。最近 5 个 pending 密钥 30 分钟内仍可确认。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `fresh` | query | 否 | boolean | true=强制生成新密钥；false=复用 30 分钟内的 pending 密钥 |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 400 | 已经绑定过 |
| 429 | 尝试过于频繁（12 次 / 5 分钟） |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/master/api/enroll/start?fresh=False' \
  -H 'Cookie: pp_master=<session>'
```

### `POST /master/api/enroll/confirm`

**确认绑定 TOTP**

校验 6 位验证码；成功后写入 master_totp_secret 并下发 7 天 HttpOnly 登录 Cookie。

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `code` | string | 是 | Google Authenticator 当前的 6 位数字 |

```json
{
  "code": "123456"
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 400 | 没有 pending 绑定或验证码错误 |
| 429 | 尝试过于频繁 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/master/api/enroll/confirm' \
  -H 'Cookie: pp_master=<session>' \
  -H 'Content-Type: application/json' \
  -d '{
  "code": "123456"
}'
```

### `POST /master/api/login`

**验证码登录**

用 Google Authenticator 当前 6 位码登录，成功下发 7 天 HttpOnly Cookie。

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `code` | string | 是 | Google Authenticator 当前的 6 位数字 |

```json
{
  "code": "123456"
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 400 | 尚未绑定或验证码错误 |
| 429 | 尝试过于频繁 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/master/api/login' \
  -H 'Cookie: pp_master=<session>' \
  -H 'Content-Type: application/json' \
  -d '{
  "code": "123456"
}'
```

### `POST /master/api/logout`

**退出登录**

清除 pp_master Cookie。

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |

```bash
curl -X POST 'http://127.0.0.1:8788/master/api/logout' \
  -H 'Cookie: pp_master=<session>'
```

### `GET /master/api/overview`

**看板总览**

返回账号数、积分、可产条数、任务统计、视频占用、磁盘与清理状态。

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录或 Cookie 过期 |

```bash
curl -X GET 'http://127.0.0.1:8788/master/api/overview' \
  -H 'Cookie: pp_master=<session>'
```

### `GET /master/api/jobs`

**任务列表**

按创建时间倒序返回任务，可按状态过滤，最多 300 条。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `limit` | query | 否 | integer | 返回条数，1–300 |
| `status` | query | 否 | string | queued / running / completed / failed / canceled |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录或 Cookie 过期 |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/master/api/jobs?limit=50&status=completed' \
  -H 'Cookie: pp_master=<session>'
```

### `GET /master/api/jobs/{jid}`

**任务详情 + 事件**

返回任务完整字段、最近 40 条事件，以及本地视频是否仍存在。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `jid` | path | 是 | string | 任务 ID |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |
| 404 | 任务不存在 |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/master/api/jobs/job_demo123' \
  -H 'Cookie: pp_master=<session>'
```

### `GET /master/api/jobs/{jid}/video`

**播放本地成片**

在面板中直接播放服务器本地 mp4。文件已被清理时返回 404。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `jid` | path | 是 | string | 任务 ID |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |
| 404 | 视频文件不可用 |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/master/api/jobs/job_demo123/video' \
  -H 'Cookie: pp_master=<session>'
```

### `GET /master/api/keys`

**Key 列表**

返回全部 Key 的元数据（不含明文）。

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |

```bash
curl -X GET 'http://127.0.0.1:8788/master/api/keys' \
  -H 'Cookie: pp_master=<session>'
```

### `POST /master/api/keys`

**创建 Key**

创建新的 Bearer Key；明文只在本次响应返回。

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 否 | Key 名称 |
| `quota` | integer | 否 | 累计任务额度；0=不限 |
| `expires_in_days` | integer | 否 | 有效天数；null=永久 |

```json
{
  "name": "client-a",
  "quota": 0,
  "expires_in_days": 30
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/master/api/keys' \
  -H 'Cookie: pp_master=<session>' \
  -H 'Content-Type: application/json' \
  -d '{
  "name": "client-a",
  "quota": 0,
  "expires_in_days": 30
}'
```

### `DELETE /master/api/keys/{kid}`

**撤销 Key**

将 Key 标记为失效，历史任务保留。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `kid` | path | 是 | string | Key ID |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |
| 422 | Validation Error |

```bash
curl -X DELETE 'http://127.0.0.1:8788/master/api/keys/key_demo123' \
  -H 'Cookie: pp_master=<session>'
```

### `GET /master/api/accounts`

**账号列表**

按积分倒序返回账号状态、冷却时间、成功/失败计数，最多 500 条。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `limit` | query | 否 | integer | 返回条数，1–500 |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/master/api/accounts?limit=50' \
  -H 'Cookie: pp_master=<session>'
```

### `GET /master/api/settings`

**读取清理设置**

返回保留天数、总量上限与自动清理开关。

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |

```bash
curl -X GET 'http://127.0.0.1:8788/master/api/settings' \
  -H 'Cookie: pp_master=<session>'
```

### `POST /master/api/settings`

**保存清理设置**

更新保留天数、总量上限与自动清理开关；返回保存后的完整设置。

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `retention_days` | number | 否 | 保留天数；0=不按时间删除 |
| `max_gb` | number | 否 | 视频总量上限 GB；0=不限 |
| `enabled` | boolean | 否 | 是否启用后台自动清理 |

```json
{
  "retention_days": 7,
  "max_gb": 20,
  "enabled": true
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/master/api/settings' \
  -H 'Cookie: pp_master=<session>' \
  -H 'Content-Type: application/json' \
  -d '{
  "retention_days": 7,
  "max_gb": 20,
  "enabled": true
}'
```

### `POST /master/api/sweep`

**执行 / 预览清理**

按保留天数、总量上限与低磁盘策略扫描视频。dry_run=true 只预览，不删除文件。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `dry_run` | query | 否 | boolean | true=只预览；false=真正删除 |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 未登录 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/master/api/sweep?dry_run=True' \
  -H 'Cookie: pp_master=<session>'
```

### `GET /v1/models`

**模型列表**

返回当前可用的模型、输出规格与积分成本。需要 Bearer Key。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `authorization` | header | 否 | string | Bearer API Key，格式：Bearer sk-pp-REPLACE_ME |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 缺少或无效的 API Key |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/v1/models' \
  -H 'Authorization: Bearer sk-pp-REPLACE_ME'
```

### `POST /v1/videos`

**创建视频任务**

创建一条异步视频任务，立即返回任务对象（status=queued）。
服务端按账号积分自动选号；单条 30 秒约消耗 120 积分。
带 reference_image/video/audio 时平台通常限制 10–20 秒；30 秒请使用纯文本提示词。
每个 Key 默认最多 2 个进行中的任务。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `authorization` | header | 否 | string | Bearer API Key，格式：Bearer sk-pp-REPLACE_ME |

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `model` | string | 否 | 模型 ID 或别名。可选 pippit-smart-1080p-30s（默认）、pippit-smart-720p-30s、pippit-smart-1080p、seedance-2.0-smart-1080p-30s、smart、auto。 |
| `prompt` | string | 是 | 视频提示词。建议写明主体、动作、镜头、光线与风格；纯文本模式可生成 30 秒。 |
| `seconds` | integer | 否 | 视频时长（秒），5–30。与 duration 二选一；都不传默认 30。带参考素材时平台通常只接受 10–20 秒。 |
| `duration` | integer | 否 | seconds 的别名，兼容部分 OpenAI SDK。 |
| `size` | string | 否 | 输出尺寸。支持 1080x1920、1920x1080、1080x1080、720x1280、1280x720、720x720；会据此推导 ratio。 |
| `resolution` | string | 否 | 清晰度：720p 或 1080p。不传则按模型默认值。 |
| `ratio` | string | 否 | 画面比例：9:16、16:9、1:1。size 能识别时以 size 为准。 |
| `n` | integer | 否 | 生成条数。当前只支持 1。 |
| `metadata` | object | 否 | 调用方自定义元数据，仅随任务保存，不影响生成。 |
| `reference_image_url` | string | 否 | 参考图：http(s) URL 或服务器本地绝对路径。用于图生视频 / 首帧参考。 |
| `reference_video_url` | string | 否 | 参考视频：http(s) URL 或服务器本地绝对路径。用于动作 / 风格参考。 |
| `reference_audio_url` | string | 否 | 参考音频：http(s) URL 或服务器本地绝对路径。必须同时提供参考图或参考视频，否则 400。 |
| `reference_image` | string | 否 | reference_image_url 的简写别名。 |
| `reference_video` | string | 否 | reference_video_url 的简写别名。 |
| `reference_audio` | string | 否 | reference_audio_url 的简写别名。 |

```json
{
  "model": "pippit-smart-1080p-30s",
  "prompt": "a cinematic sunrise over a misty lake, slow push-in, no text",
  "seconds": 30,
  "duration": 30,
  "size": "1080x1920",
  "resolution": "1080p",
  "ratio": "9:16",
  "n": 1,
  "metadata": {
    "client": "demo",
    "trace_id": "abc-123"
  },
  "reference_image_url": "https://example.com/ref.jpg",
  "reference_video_url": "https://example.com/ref.mp4",
  "reference_audio_url": "https://example.com/ref.mp3",
  "reference_image": "https://example.com/ref.jpg",
  "reference_video": "https://example.com/ref.mp4",
  "reference_audio": "https://example.com/ref.mp3"
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | 任务对象，含 id、status、progress、seconds、size |
| 400 | 参数错误（时长、比例、参考素材组合不合法等） |
| 401 | 缺少或无效的 API Key |
| 429 | Key 并发或额度超限 |
| 503 | 号池没有账号能负担本次积分 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/v1/videos' \
  -H 'Authorization: Bearer sk-pp-REPLACE_ME' \
  -H 'Content-Type: application/json' \
  -d '{
  "model": "pippit-smart-1080p-30s",
  "prompt": "a cinematic sunrise over a misty lake, slow push-in, no text",
  "seconds": 30,
  "duration": 30,
  "size": "1080x1920",
  "resolution": "1080p",
  "ratio": "9:16",
  "n": 1,
  "metadata": {
    "client": "demo",
    "trace_id": "abc-123"
  },
  "reference_image_url": "https://example.com/ref.jpg",
  "reference_video_url": "https://example.com/ref.mp4",
  "reference_audio_url": "https://example.com/ref.mp3",
  "reference_image": "https://example.com/ref.jpg",
  "reference_video": "https://example.com/ref.mp4",
  "reference_audio": "https://example.com/ref.mp3"
}'
```

### `GET /v1/videos`

**查询任务列表**

按创建时间倒序返回当前 Key 的任务，最多 200 条。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `limit` | query | 否 | integer | 返回条数，1–200 |
| `authorization` | header | 否 | string | Bearer API Key，格式：Bearer sk-pp-REPLACE_ME |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 缺少或无效的 API Key |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/v1/videos?limit=50' \
  -H 'Authorization: Bearer sk-pp-REPLACE_ME'
```

### `POST /v1/video/generations`

**创建视频任务（OpenAI 别名）**

与 POST /v1/videos 完全等价，兼容部分 OpenAI 风格 SDK。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `authorization` | header | 否 | string | Bearer API Key，格式：Bearer sk-pp-REPLACE_ME |

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `model` | string | 否 | 模型 ID 或别名。可选 pippit-smart-1080p-30s（默认）、pippit-smart-720p-30s、pippit-smart-1080p、seedance-2.0-smart-1080p-30s、smart、auto。 |
| `prompt` | string | 是 | 视频提示词。建议写明主体、动作、镜头、光线与风格；纯文本模式可生成 30 秒。 |
| `seconds` | integer | 否 | 视频时长（秒），5–30。与 duration 二选一；都不传默认 30。带参考素材时平台通常只接受 10–20 秒。 |
| `duration` | integer | 否 | seconds 的别名，兼容部分 OpenAI SDK。 |
| `size` | string | 否 | 输出尺寸。支持 1080x1920、1920x1080、1080x1080、720x1280、1280x720、720x720；会据此推导 ratio。 |
| `resolution` | string | 否 | 清晰度：720p 或 1080p。不传则按模型默认值。 |
| `ratio` | string | 否 | 画面比例：9:16、16:9、1:1。size 能识别时以 size 为准。 |
| `n` | integer | 否 | 生成条数。当前只支持 1。 |
| `metadata` | object | 否 | 调用方自定义元数据，仅随任务保存，不影响生成。 |
| `reference_image_url` | string | 否 | 参考图：http(s) URL 或服务器本地绝对路径。用于图生视频 / 首帧参考。 |
| `reference_video_url` | string | 否 | 参考视频：http(s) URL 或服务器本地绝对路径。用于动作 / 风格参考。 |
| `reference_audio_url` | string | 否 | 参考音频：http(s) URL 或服务器本地绝对路径。必须同时提供参考图或参考视频，否则 400。 |
| `reference_image` | string | 否 | reference_image_url 的简写别名。 |
| `reference_video` | string | 否 | reference_video_url 的简写别名。 |
| `reference_audio` | string | 否 | reference_audio_url 的简写别名。 |

```json
{
  "model": "pippit-smart-1080p-30s",
  "prompt": "a cinematic sunrise over a misty lake, slow push-in, no text",
  "seconds": 30,
  "duration": 30,
  "size": "1080x1920",
  "resolution": "1080p",
  "ratio": "9:16",
  "n": 1,
  "metadata": {
    "client": "demo",
    "trace_id": "abc-123"
  },
  "reference_image_url": "https://example.com/ref.jpg",
  "reference_video_url": "https://example.com/ref.mp4",
  "reference_audio_url": "https://example.com/ref.mp3",
  "reference_image": "https://example.com/ref.jpg",
  "reference_video": "https://example.com/ref.mp4",
  "reference_audio": "https://example.com/ref.mp3"
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 400 | 参数错误 |
| 401 | 缺少或无效的 API Key |
| 429 | Key 并发或额度超限 |
| 503 | 号池没有账号能负担本次积分 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/v1/video/generations' \
  -H 'Authorization: Bearer sk-pp-REPLACE_ME' \
  -H 'Content-Type: application/json' \
  -d '{
  "model": "pippit-smart-1080p-30s",
  "prompt": "a cinematic sunrise over a misty lake, slow push-in, no text",
  "seconds": 30,
  "duration": 30,
  "size": "1080x1920",
  "resolution": "1080p",
  "ratio": "9:16",
  "n": 1,
  "metadata": {
    "client": "demo",
    "trace_id": "abc-123"
  },
  "reference_image_url": "https://example.com/ref.jpg",
  "reference_video_url": "https://example.com/ref.mp4",
  "reference_audio_url": "https://example.com/ref.mp3",
  "reference_image": "https://example.com/ref.jpg",
  "reference_video": "https://example.com/ref.mp4",
  "reference_audio": "https://example.com/ref.mp3"
}'
```

### `GET /v1/videos/{vid}`

**查询单个任务**

轮询任务状态与进度。status=completed 后可下载；failed 时带 error 字段。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `vid` | path | 是 | string | 任务 ID，即 POST /v1/videos 返回的 id |
| `authorization` | header | 否 | string | Bearer API Key，格式：Bearer sk-pp-REPLACE_ME |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 缺少或无效的 API Key |
| 404 | 任务不存在，或不属于当前 Key |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/v1/videos/video_demo123' \
  -H 'Authorization: Bearer sk-pp-REPLACE_ME'
```

### `GET /v1/videos/{vid}/content`

**下载视频（支持 Range）**

返回 mp4 文件。支持 HTTP Range（206 分段下载）。本地文件被自动清理后返回 410。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `vid` | path | 是 | string | 任务 ID |
| `authorization` | header | 否 | string | Bearer API Key，格式：Bearer sk-pp-REPLACE_ME |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | 完整 mp4 |
| 206 | Range 分段内容 |
| 401 | 缺少或无效的 API Key |
| 404 | 任务不存在，或不属于当前 Key |
| 409 | 任务尚未完成 |
| 410 | 本地文件已被自动清理 |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/v1/videos/video_demo123/content' \
  -H 'Authorization: Bearer sk-pp-REPLACE_ME'
```

### `HEAD /v1/videos/{vid}/content`

**探测视频（HEAD）**

只返回响应头，包含 Content-Length 与 Accept-Ranges: bytes，可用于下载前探测大小。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `vid` | path | 是 | string | 任务 ID |
| `authorization` | header | 否 | string | Bearer API Key，格式：Bearer sk-pp-REPLACE_ME |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | 文件可下载 |
| 401 | 缺少或无效的 API Key |
| 404 | 任务不存在，或不属于当前 Key |
| 409 | 任务尚未完成 |
| 410 | 本地文件已被自动清理 |
| 422 | Validation Error |

```bash
curl -X HEAD 'http://127.0.0.1:8788/v1/videos/video_demo123/content' \
  -H 'Authorization: Bearer sk-pp-REPLACE_ME'
```

### `POST /v1/videos/{vid}/cancel`

**取消排队中的任务**

只能取消 status=queued 的任务；已经开始的生成任务返回 409。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `vid` | path | 是 | string | 任务 ID |
| `authorization` | header | 否 | string | Bearer API Key，格式：Bearer sk-pp-REPLACE_ME |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 缺少或无效的 API Key |
| 404 | 任务不存在，或不属于当前 Key |
| 409 | 任务已经开始，不能取消 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/v1/videos/video_demo123/cancel' \
  -H 'Authorization: Bearer sk-pp-REPLACE_ME'
```

### `POST /admin/keys`

**创建 API Key**

创建一个新的 Bearer Key。明文只在本次响应返回，请立即保存。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `x-admin-token` | header | 否 | string | 管理令牌，值见服务器 data/admin_token.txt |
| `authorization` | header | 否 | string | 也可以用 Bearer <admin_token> 代替 X-Admin-Token |

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 否 | Key 名称，便于区分调用方 |
| `quota` | integer | 否 | 累计任务额度；0 表示不限 |
| `expires_in_days` | integer | 否 | 有效天数；null 表示永不过期 |

```json
{
  "name": "client-a",
  "quota": 0,
  "expires_in_days": 30
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 管理令牌错误 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/admin/keys' \
  -H 'X-Admin-Token: <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
  "name": "client-a",
  "quota": 0,
  "expires_in_days": 30
}'
```

### `GET /admin/keys`

**Key 列表**

返回全部 Key 的元数据（不含明文）。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `x-admin-token` | header | 否 | string | 管理令牌，值见服务器 data/admin_token.txt |
| `authorization` | header | 否 | string | 也可以用 Bearer <admin_token> 代替 X-Admin-Token |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 管理令牌错误 |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/admin/keys' \
  -H 'X-Admin-Token: <admin_token>'
```

### `DELETE /admin/keys/{kid}`

**撤销 Key**

将 Key 标记为失效，历史任务保留。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `kid` | path | 是 | string | Key ID |
| `x-admin-token` | header | 否 | string | 管理令牌，值见服务器 data/admin_token.txt |
| `authorization` | header | 否 | string | 也可以用 Bearer <admin_token> 代替 X-Admin-Token |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 管理令牌错误 |
| 422 | Validation Error |

```bash
curl -X DELETE 'http://127.0.0.1:8788/admin/keys/key_demo123' \
  -H 'X-Admin-Token: <admin_token>'
```

### `POST /admin/accounts/import`

**导入账号**

导入一个或多个号池账号；bootstrap=true 时会立即登录平台校验并刷新积分。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `x-admin-token` | header | 否 | string | 管理令牌，值见服务器 data/admin_token.txt |
| `authorization` | header | 否 | string | 也可以用 Bearer <admin_token> 代替 X-Admin-Token |

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `accounts` | array | 否 | 账号对象数组。每个对象至少包含 uid 与 cookies；可选 ports、tag、workspace_id、space_id、credit。 |
| `paths` | array | 否 | 服务器本地 JSON 文件路径数组。文件内容可以是账号对象、账号数组或指向账号文件的路径。 |
| `bootstrap` | boolean | 否 | 是否立即登录平台并拉取 workspace/space/积分 |
| `min_credit` | integer | 否 | 只保留积分不低于该值的导入结果 |

```json
{
  "accounts": [
    {
      "uid": "123456",
      "cookies": {
        "sessionid": "..."
      },
      "ports": [
        22030,
        22031,
        22032
      ]
    }
  ],
  "paths": [
    "/opt/pippit-pool/data/accounts.json"
  ],
  "bootstrap": true,
  "min_credit": 0
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 400 | 没有解析到任何账号 |
| 401 | 管理令牌错误 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/admin/accounts/import' \
  -H 'X-Admin-Token: <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
  "accounts": [
    {
      "uid": "123456",
      "cookies": {
        "sessionid": "..."
      },
      "ports": [
        22030,
        22031,
        22032
      ]
    }
  ],
  "paths": [
    "/opt/pippit-pool/data/accounts.json"
  ],
  "bootstrap": true,
  "min_credit": 0
}'
```

### `GET /admin/accounts`

**账号列表**

返回账号状态、积分、最近错误与任务计数；cookies 只显示数量。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `x-admin-token` | header | 否 | string | 管理令牌，值见服务器 data/admin_token.txt |
| `authorization` | header | 否 | string | 也可以用 Bearer <admin_token> 代替 X-Admin-Token |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 管理令牌错误 |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/admin/accounts' \
  -H 'X-Admin-Token: <admin_token>'
```

### `POST /admin/accounts/refresh`

**刷新账号积分**

重新登录平台读取积分；claim=true 时先领取每日任务奖励。可指定 uids，默认全部。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `x-admin-token` | header | 否 | string | 管理令牌，值见服务器 data/admin_token.txt |
| `authorization` | header | 否 | string | 也可以用 Bearer <admin_token> 代替 X-Admin-Token |

**请求体字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `uids` | array | 否 | 只刷新指定 uid；不传则刷新全部账号 |
| `claim` | boolean | 否 | 刷新前先领取每日任务奖励（claim_all） |

```json
{
  "uids": [
    "123456"
  ],
  "claim": false
}
```

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 管理令牌错误 |
| 422 | Validation Error |

```bash
curl -X POST 'http://127.0.0.1:8788/admin/accounts/refresh' \
  -H 'X-Admin-Token: <admin_token>' \
  -H 'Content-Type: application/json' \
  -d '{
  "uids": [
    "123456"
  ],
  "claim": false
}'
```

### `GET /admin/jobs`

**全部任务**

返回全号池任务，按创建时间倒序，最多 500 条。

| 名称 | 位置 | 必填 | 类型 | 说明 |
|---|---|---|---|---|
| `limit` | query | 否 | integer | 返回条数，1–500 |
| `x-admin-token` | header | 否 | string | 管理令牌，值见服务器 data/admin_token.txt |
| `authorization` | header | 否 | string | 也可以用 Bearer <admin_token> 代替 X-Admin-Token |

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |
| 401 | 管理令牌错误 |
| 422 | Validation Error |

```bash
curl -X GET 'http://127.0.0.1:8788/admin/jobs?limit=50' \
  -H 'X-Admin-Token: <admin_token>'
```

### `GET /healthz`

**健康检查**

返回服务存活状态、服务器时间与 admin token 文件位置。无需鉴权。

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |

```bash
curl -X GET 'http://127.0.0.1:8788/healthz'
```

### `GET /`

**服务信息**

返回服务入口、文档地址与面板地址。无需鉴权。

**响应码**

| 状态码 | 说明 |
|---|---|
| 200 | Successful Response |

```bash
curl -X GET 'http://127.0.0.1:8788/'
```

## 错误码

| 状态码 | 含义 |
|---|---|
| 400 | 参数错误、参考素材组合不合法 |
| 401 | 缺少或无效的鉴权 |
| 404 | 任务/Key 不存在或不属于当前 Key |
| 409 | 视频未就绪或任务不可取消 |
| 410 | 本地文件已被自动清理 |
| 422 | 请求体校验失败 |
| 429 | Key 并发或额度超限 |
| 503 | 号池没有足够积分的账号 |

## 任务状态与进度

| status | 含义 |
|---|---|
| `queued` | 排队中，可取消 |
| `in_progress` | 生成中 |
| `completed` | 已完成，可下载 |
| `failed` | 失败，`error.message` 有原因 |
| `canceled` | 已取消 |

进度参考：queued 0 → 提交 10 → 生成中 40 → 素材就绪 80 → 探测 95 → 完成 100。
