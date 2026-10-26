# SMS Gate

> 卡密制短信验证码接收与分发中台。
> 授权链接卡 · 直链卡 · 多号段 · 号码池 · TOTP 后台 · 全量审计

## 功能

- 授权链接卡：`https://your-domain.com/c/XXXX-XXXX-XXXX-XXXX`
- 直链卡：`卡号:+12025550123----https://your-domain.com/smsrecord?token=...`
- 号段：`us` / `gb` / `hk` / `cn_real` / `cn_virtual`
- 号源：NexSMS、d1jiema、SMS-Activate、5SIM、自建号码池
- 管理后台：批量生成（1–500）、次数、有效期、备注、导出、禁用
- 直链卡批次导出
- 号码池批量导入 `手机号----上游取码地址`
- 数据看板：兑换、接码明细、成功率
- 全量审计：登录、兑换、取号、收码、封禁、配置变更
- 单 IP / CIDR 封禁、跨进程精确限流
- TOTP 六位数登录、CSRF、CSP、HSTS、SSRF 防护

## 技术栈

- Python 3.12 + FastAPI + Uvicorn
- SQLite WAL
- Jinja2 + 原生 JS/CSS，无构建步骤
- systemd + nginx + certbot

## 目录

```text
sms-gate/
├─ app/
│  ├─ main.py         路由装配、启动、janitor
│  ├─ public_api.py   前台授权 / 取号 / 轮询取码
│  ├─ admin_api.py    后台接口
│  ├─ relay_api.py    直链接码接口
│  ├─ providers.py    号源抽象
│  ├─ d1jiema.py      国内实卡/虚卡
│  ├─ nexq.py         NexSMS 客户端
│  ├─ db.py           SQLite schema
│  └─ security.py     会话、CSRF、TOTP、限流、审计
├─ deploy/            systemd / nginx 模板
└─ tests/             回归与并发测试
```

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

mkdir -p data logs
uvicorn app.main:app --host 127.0.0.1 --port 8861
```

首次启动生成 `data/setup.token`：

```text
http://127.0.0.1:8861/admin/setup?t=<setup-token>
```

创建管理员 → Google Authenticator 扫码 → 输入 6 位码完成绑定。令牌一次性，绑定后失效。

## 配置

通过环境变量或本地未跟踪配置注入：

| 变量 | 说明 |
|---|---|
| `SMSGATE_SECRET` | 会话签名密钥 |
| `SMSGATE_DB` | SQLite 路径 |
| `SMSGATE_BASE_URL` | 对外基础 URL，例如 `https://your-domain.com` |
| `NEXSMS_KEY` | NexSMS 密钥（可选） |
| `D1JIEMA_TOKEN` | d1jiema 令牌（可选） |
| `SMSACTIVATE_KEY` | SMS-Activate 密钥（可选） |
| `FIVESIM_KEY` | 5SIM 密钥（可选） |

真实密钥不要写入仓库；使用 `.env` 或 systemd `EnvironmentFile`。

## 卡密格式

授权链接卡：

```text
https://your-domain.com/c/XXXX-XXXX-XXXX-XXXX
```

直链卡：

```text
卡号:+12025550123----https://your-domain.com/smsrecord?token=0123456789abcdef0123456789abcdef
```

卡密以 SHA-256 落库；明文只在生成时返回一次。

## 部署

```bash
python3.12 -m venv /opt/smsgate/venv
/opt/smsgate/venv/bin/pip install -r requirements.txt
cp deploy/smsgate.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now smsgate
cp deploy/nginx-smsgate-domain.conf /etc/nginx/conf.d/smsgate.conf
nginx -t && systemctl reload nginx
certbot --nginx -d your-domain.com
```

生产建议：`uvicorn --workers 4 --limit-concurrency 400 --backlog 2048`，仅监听 `127.0.0.1`。

## 安全

- 管理员密码 PBKDF2-SHA256（24 万次迭代），TOTP 密钥加密落库
- 会话 HttpOnly + SameSite=Lax + HTTPS Secure
- 写操作全量 CSRF
- 登录防撞库：账号/IP 双维度锁定 + 等时校验 + 随机延迟
- 跨进程精确限流（SQLite 计数表）
- 号段选择原子锁 + 参数白名单
- 输出脱敏：上游单价、余额、消息 ID 不下发
- SSRF 防护、CSP、HSTS、`X-Frame-Options: DENY`

## 性能

单机 4 worker 实测：120 并发 / 120 成功 / 200 全绿，p95 = 0.72s。
号源到达率随上游波动；国内实卡约 91%、英国约 73%、香港约 61%。

## 测试

```bash
python tests/test_smoke.py
python tests/test_relay.py
python tests/test_abuse.py
python tests/test_concurrency.py
python tests/test_retry.py
python tests/test_security_exit.py
```

## 免责声明

仅用于自有账号或已获得明确授权的短信验证码接收场景。使用者需遵守当地法律与目标平台服务条款。
