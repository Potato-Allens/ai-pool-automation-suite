# `/openapi.json` 情报摘录（Auth Console v0.1.0）

`GET https://auth.vobilemax.cn/openapi.json` — **200，无鉴权**。下面是批量注册用得上的部分（原文来自该文件，2026-10-26 抓取）。

> 测试环境 `https://test-auth.vobilemax.cn/openapi.json` 同样无鉴权，41 条路径与生产**逐条一致**，可直接用同一套客户端联调。

## 1. 路径清单（41 条）

```
GET    /health/live
GET    /health/ready
GET    /api/auth/captcha-config
GET    /api/auth/agreements
POST   /api/auth/agreements/accept
GET    /api/auth/agreements/my-status
GET    /agreements/{agreement_type}
GET    /terms
GET    /privacy
POST   /api/internal/users/batch-import              <- 批量建号
POST   /api/internal/users/{user_id}/reset-password  <- 后台改密
POST   /api/internal/account-risk/users/{uid}/{action}
POST   /api/internal/account-risk/batches
GET    /api/internal/account-risk/batches/{batch_id}
POST   /api/internal/account-risk/batches/{batch_id}/retry
GET    /login
POST   /login
POST   /api/auth/login
POST   /login/send-code
POST   /register/send-code
POST   /reset-password/send-code
POST   /login/sms
POST   /register
POST   /reset-password
GET    /session
POST   /session/logout
POST   /session/logout-all
GET    /logout
POST   /api/auth/register
GET    /api/auth/sms-config
POST   /api/auth/send-sms-code
POST   /api/auth/login/send-code
POST   /api/auth/login-sms
POST   /api/auth/reset-password/send-code
POST   /api/auth/reset-password
POST   /api/me/password
POST   /api/me/reset-password/send-code
POST   /api/me/reset-password
POST   /api/auth/phone/change/send-code
POST   /api/auth/phone/change
GET    /authorize
POST   /api/auth/oauth/token
```

## 2. 关键 schema

### BatchImportRequest / BatchImportUserRequest

```json
{
  "batch_id": "string(1..128)      // 幂等批次",
  "operator": "string(1..128)",
  "audit_id": "string(1..128)",
  "users": [                       // 1..1000
    {
      "phone": "string(1..32)",
      "username": "string(1..128)",
      "password": "string(1..128)",
      "initial_credits": "0",      // 仅支持 0
      "register_source": "string?(≤32)",
      "send_invitation": false     // 仅支持 false
    }
  ]
}
```

响应：

```json
{"batch_id":"...",
 "results":[{"status":"CREATED|FAILED|NOT_READY","user_id":123|null,"error":"稳定错误码"|null}]}
```

### RegisterRequest（`POST /api/auth/register`，JSON）

openapi `required`：`username(2..128)`、`password(8..128)`、`phone(1..32)` —— **只有这 3 个**（R131 活体核对）
业务必填但**不在** schema required 里（缺则 400）：`sms_code`、`captcha_platform` + `captcha_verify_param`、`accepted_agreement_ids`
可选：`invitation_code`、`email`、`real_name`、`id_card`、`birthday`、`id_card_valid_from/to`、`id_card_long_term`、`address`

响应 `AuthResponse{access_token, token_type="bearer", user:UserOut}`。

`UserOut`：`id`/`uid`(对外标识)/`username`(脱敏)/`nickname`/`phone`(脱敏)/`area_type`/`birthday`…

### SendFormCodeRequest（`/register/send-code`）/ SendFormCodeResponse

```json
{"phone":"...", "client_id":"dreamstudio", "locale":"zh-CN|en-US", "captcha_verify_param":"≤8192"}
-> {"ok":bool, "message":"...", "code":null}   // code 仅联调环境回传
```

### SendSmsCodeRequest（`/api/auth/send-sms-code`）

```json
{"phone":"...", "captcha_platform":"dreammaker", "captcha_verify_param":"≤8192"}
-> {"sent":bool, "mock":bool, "code":null, "message":"验证码已发送"}
```

> 逻辑：手机号已注册 → 发 login 用途码；未注册 → 发 register 用途码。
> 测试环境实测：`{"sent":true,"mock":false,"code":null}` —— **没有 mock 后门，code 也不回传**。

### TokenRequest / TokenResponse

```json
{"grant_type":"authorization_code","code":"...","client_id":"dreamstudio",
 "redirect_uri":"https://dreammaker.vobilemax.cn/auth/callback","code_verifier":"43..128 [A-Za-z0-9._~-]"}
-> {"access_token":"<HS256 JWT>","token_type":"bearer","expires_in":<int>}
```

### CaptchaConfigOut

```json
{"enabled":true,"region":"cn","prefix":"8vo785","scene_id":"leukcmsx"}
```

### AgreementOut

```json
{"id":12,"type":"terms","version":"2026.09.30","title":"用户协议","content":"<全文>"}
```

## 3. 错误约定

| 形态 | 含义 |
|---|---|
| `422 {"detail":[{...pydantic...}]}` | FastAPI 参数校验 |
| `{"detail":"内部认证凭证无效"}` 401 | 内部端点缺/错 `X-Internal-Token` |
| `{"detail":"Captcha verification rejected"}` | 阿里云服务端校验未通过 |
| `{"detail":"invalid OAuth client"}` 400 | `client_id` 不在白名单 |
| `{"detail":"invalid authorization code"}` 400 | code 已用/不存在 |
| `200 {"ok":false,...}` | 发码业务失败（不抛 HTTP 错） |
| 409（两个注册端点） | openapi **只有 description**「注册唯一字段已被占用」、**没有 body schema**；真实 body 至今未实测（PROTOCOL §2.5 / §5.2.2） |

## 4. 生产 vs 测试环境

| 项 | 生产 | 测试 |
|---|---|---|
| 域名 | `auth.vobilemax.cn` | `test-auth.vobilemax.cn`（<TEST_ENV_IP>） |
| openapi 路径数 | 41 | 41（一致） |
| 协议 id | 12(terms) / 13(privacy) | **11 / 12** |
| 回调白名单 | `https://dreammaker.vobilemax.cn/auth/callback` | `https://test-dreammaker.vobilemax.cn/auth/callback`（唯一） |
| `client_id` | `dreamstudio` | `dreamstudio` |
| captcha | `8vo785` / `leukcmsx` | 同 |
| 发码 | 真短信 | 真短信（同运营商校验） |

客户端切环境：

```python
from vmax_client import VmaxAuth
cli = VmaxAuth(base="https://test-auth.vobilemax.cn",
               redirect_uri="https://test-dreammaker.vobilemax.cn/auth/callback")
```

或命令行 `--base/--redirect`、环境变量 `VMAX_BASE` / `VMAX_REDIRECT_URI`。

## 5. 情报价值

1. 直接给出内部批量开户接口的完整契约（字段、上限、幂等语义），省掉从 bot 流量里猜
2. 暴露 `sms-config`、`captcha-config`、`agreements` 这类「前端配置」端点，全部无鉴权
3. `TokenResponse.expires_in` + HS256 JWT → token 可离线解析 `exp`/`sub`，便于池化管理
4. `account-risk` 系列说明站点有「批量注册风控批次」机制，ban/unban 按 8 位 uid 操作
