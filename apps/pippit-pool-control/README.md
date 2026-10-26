# Pippit Pool Control

> 公网控制面 + 远程 worker 的 Pippit 号池部署形态。
> 控制面负责任务、Key、配额和结果；worker 负责浏览器/协议渲染与回传。

## 功能

- 管理端：Google Authenticator 六位数登录、Key 创建/吊销、任务列表、号池状态
- 任务：提交、排队、取消、进度、结果下载
- Worker：领取任务、心跳、成功回传 mp4、失败重排
- 账号池：积分、冷却、并发、失败换号
- 存储：SQLite WAL；结果文件可落本地或对象存储
- 部署：控制面与 worker 可分离，适合内网 worker + 公网 API

## 目录

```text
pippit-pool-control/
├─ server.py          FastAPI 控制面
├─ db.py              SQLite schema / 任务 / Key / 会话
├─ pool.py            账号池与调度
├─ worker.py          本地 worker
├─ worker_remote.py   远程 worker
├─ totp.py            TOTP 绑定与校验
├─ recover.py         崩溃恢复
├─ admin.html         管理面板
├─ client_example.py  调用示例
└─ run_api.py         本地启动
```

## 快速开始

```bash
pip install fastapi uvicorn requests
export API_ADMIN_TOKEN='<CHANGE_ME>'
export API_ROOT_PATH=''
python run_api.py
```

打开：

```text
http://127.0.0.1:8787/admin
```

首次绑定：填入管理令牌 → 生成二维码 → Google Authenticator 扫码 → 输入 6 位码。

## 远程 worker

```bash
export API_BASE='http://127.0.0.1:8787'
export API_ADMIN_TOKEN='<CHANGE_ME>'
python worker_remote.py
```

worker 需要本机 Chrome、可用代理出口和已导入账号。建议 worker 放在内网，通过反向代理或 VPN 访问控制面。

## 主要端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/healthz` | - | 健康检查 |
| GET | `/v1/models` | - | 模型列表 |
| POST | `/v1/keys` | Admin | 创建 Key |
| GET | `/v1/keys` | Admin | Key 列表 |
| DELETE | `/v1/keys/{id}` | Admin | 吊销 Key |
| GET | `/v1/pool/status` | Admin | 号池统计 |
| POST | `/v1/videos/generations` | Key | 提交任务 |
| GET | `/v1/videos/generations/{id}` | Key | 查询 |
| GET | `/v1/videos/generations/{id}/content` | Key | 下载 |
| POST | `/v1/worker/claim` | Worker | 领取任务 |
| POST | `/v1/worker/{id}/result` | Worker | 回传结果 |
| POST | `/v1/worker/{id}/fail` | Worker | 失败重排 |

完整字段以 `server.py` 与 OpenAPI 为准。

## 环境变量

| 变量 | 说明 |
|---|---|
| `API_ADMIN_TOKEN` | 管理令牌；首次绑定用 |
| `API_ROOT_PATH` | 子路径部署，例如 `/pippit` |
| `API_ADMIN_SESSION_TTL` | 管理会话有效期（秒） |
| `API_OUT_DIR` | 结果目录 |
| `API_MAX_WORKERS` | 并发 worker 上限 |

## 任务状态

```text
queued -> running -> succeeded | failed | canceled
```

失败信息包含 `rc=` 与产线尾部日志；余额不足会自动换号重试。

## 安全

- 管理端 TOTP + 限流；管理令牌只用于首次绑定/找回。
- Key 只存哈希；明文只返回一次。
- worker 与 API 使用独立令牌/网络边界。
- 结果目录不暴露目录列表，只允许按任务 ID 下载。
- 不提交任何真实服务器地址、Cookie、Key 或账号。

## 测试

```bash
python -m compileall .
python client_example.py --help
```

## 免责声明

仅用于自有账号、已授权账号或合规测试环境。
