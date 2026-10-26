# VMAX Bulk Register

> 面向 `auth.vobilemax.cn` 的协议级批量注册 / 登录工具链。
> 滑块 token 农场 · 实卡 Modem 收码 · 付费面板 · 批量调度 · 限流节流 · 产能规划

## 功能

- 协议链路：`send-code -> register -> OAuth code -> PKCE token`
- 滑块 token 农场：多进程预解、一次性 token、TTL 管理
- 收码：HTTP / file / cmd / prompt / 付费面板 / 实卡 Modem（AT+PDU）
- 批量：目标数量语义、断点续跑、每行即时落盘、429 立即停
- 节流：按出口 IP 计数、封禁期等待、轮询续封保护
- 规划：通过率 / 耗时 / TTL / 写限流 / 短信时延 → 账号/h、ETA、买号量
- 运维：doctor 预检、contract 契约漂移、accept 一键验收、report 汇总
- 测试：1700+ 离线断言（以仓库内测试为准）

## 目录

```text
vmax-bulk-register/
├─ vmax_client.py      协议客户端
├─ vmax_captcha.py     滑块解算
├─ vmax_tokens.py      token 农场
├─ vmax_register.py    批量注册
├─ vmax_login.py       批量登录
├─ vmax_smsd.py        收码 daemon
├─ vmax_modem.py       实卡 Modem
├─ vmax_panel.py       付费面板
├─ vmax_pacer.py       限流/封禁
├─ vmax_plan.py        产能规划
├─ vmax_contract.py    契约漂移哨兵
├─ vmax_accept.py      一键验收
├─ vmax_doctor.py      上线预检
├─ vmax_report.py      结果汇总
└─ tests/              离线与集成测试
```


## 文档

| 文件 | 内容 |
|---|---|
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | 协议逆向记录：IdP / openapi / 验证码 / 设备信任 / 注册与登录状态机 |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | 运维手册：部署、收码通道、节流、排障、扩产 |
| [`docs/CHECKLIST.md`](docs/CHECKLIST.md) | 上线检查清单与验收步骤 |
| [`docs/OPENAPI_NOTES.md`](docs/OPENAPI_NOTES.md) | `/openapi.json` 41 条路径逐条备注与生产/测试差异 |

> 文档中的 IP、域名、账号、手机号与密钥均已脱敏为占位符。

## 快速开始

```bash
pip install -r requirements.txt

# 只读预检
python vmax_doctor.py --base https://auth.vobilemax.cn

# 离线演练，不发短信、不建号
python vmax_go.py --dry-run

# 单号发码 + 注册（需要实卡/面板收码）
python vmax_register.py --phone <PHONE> --sms http://127.0.0.1:8787 --secret '<CHANGE_ME>'
```

## 环境变量

| 变量 | 说明 |
|---|---|
| `VMAX_SMS_SECRET` | 收码 daemon 共享密钥 |
| `VMAX_BASE` | 目标站点，默认 `https://auth.vobilemax.cn` |
| `VMAX_HTTP_PROXY` | 出口代理（可选） |
| `VMAX_DATA_DIR` | token / journal / 结果目录 |
| `VMAX_PANEL_KEY` | 付费面板密钥（可选） |

## 收码通道

```bash
# HTTP 中继
python vmax_smsd.py --host 127.0.0.1 --port 8787 --secret '<CHANGE_ME>'

# 实卡 Modem
python vmax_smsd.py --modem COM3 --secret '<CHANGE_ME>'
```

`vmax_smsd` 支持长轮询、`since` 亚秒精度、TTL、拒绑；`vmax_modem` 支持 GSM7/UCS2/UDH 拼接、开机补收、断线重连。

## 限流与产能

- 注册写接口按出口 IP 计数，持续约 30 写/h/IP；429 首封 ≥600s，轮询会续封。
- 滑块单次通过率约 35%（采样值），token 有效期约 90–120s，用一次即废。
- 同号重发冷却约 60s；单码尝试预算 5 次。
- 多出口可线性放大写墙；`vmax_plan.py --ips N` 可估算。

## 测试

```bash
python -m pytest tests -q
python vmax_docaudit.py
```

## 安全

- 仓库不包含真实手机号、短信、账号、Cookie、面板密钥或代理账密。
- 测试使用 `<TEST_PHONE>` / `<TEST_SECRET>` 等占位符。
- 生产环境使用独立 `.env`，不要提交。
- 仅用于自有账号、已授权账号或合规测试环境。

## 免责声明

协议和站点可能随时变更；使用前先跑 `vmax_doctor` 与 `vmax_contract`，并遵守目标平台服务条款与当地法律。
