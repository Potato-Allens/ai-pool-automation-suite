# 实卡到货 · 30 分钟验收清单（vmax 批量注册）

> 适用前提：已有一张**真实 +86 实卡**（移动/联通/电信/广电，非 170/171 虚商段）。
> 目标：30 分钟内从「卡在手里」走到「1 个可用账号 + 可复现批跑」。
> 全程除第 4 步的 1 个真实验证码外，不产生任何短信费用。
> 所有命令都在 `outputs/vmax_toolkit` 目录下执行。

---

## 0. 前置检查（3 分钟）

```powershell
cd <...>\outputs\vmax_toolkit

python vmax_pacer.py status          # 必须 banned=false；有 429 就等，别轮询
python vmax_doctor.py --flows --skip-egress  # 站点改版/协议 id/scene 变化的第一道警报；--flows 还会验回收流（0 短信）
python vmax_contract.py check --baseline contract.json  # 第二道警报：字段改名/新增必填/scene 换了都会报（0 成本）
```

- [ ] 号码是 11 位大陆号，**不是 170/171**（`/register` 会直接判「未通过运营商验证」）
- [ ] 起跑用 `vmax_day` 时加上 `--contract contract.json`：契约关键漂移会在**花一分钱之前** exit 2（`verdict=contract_drift`）
- [ ] 单卡实验同理：`vmax_onecard ... --contract contract.json`（漂移/不可达 → exit 4、0 短信）；`vmax_ready --contract` 直接透传给 day
- [ ] 首次单卡跑 `vmax_verify.py`：它在 303 之后会顺手复核 `/session` 真认证与协议签署状态（缺签自动补一次，0 短信；`evidence/_r128_agreements_gate.log`）
- [ ] 机器时间已同步（`w32tm /resync`）：`since={since}` 过滤依赖 send-code 与收码端同一时钟
- [ ] 本机与手机在同一局域网（或手机能访问本机公网端口）

---

## 1. 收码通道（10 分钟，四选一）

### 方案 A（推荐）Android + SMS Forwarder
1. Play 商店装 **SMS Forwarder**（或 Tasker / MacroDroid）。
2. 规则：`收到短信` → 条件 `发件人包含 VobileMax`（或不过滤）→ 动作 `HTTP POST`
   - URL：`http://<PC局域网IP>:8787/ingest`
   - Header：`X-Sms-Secret: s3cr3t`
   - Body(JSON)：`{"phone":"%from%","text":"%text%"}`
3. 在 PC 上确认手机能连通：`curl http://<PC-IP>:8787/health`

> `%from%` 常见形态是 `+8613800138000` / `1069...`，daemon 会自动剥掉 `+86`、空格、短横线；
> 但**发件人必须是真实号码**，用 106 通道时请让 app 把「本机号码」写进 body 的 `phone` 字段。

### 方案 B iOS 快捷指令
自动化 →「收到信息」→ 获取快捷指令输入 → 「获取 URL 内容」POST JSON（同上 URL/Header/Body）。
iOS 拿不到收件号码时，固定填你那张卡的号码。

### 方案 C 硬件模块（SIM800C / EC20 / Quectel）
```python
# 轮询 AT+CMGL="ALL"，把每条 URC 转发到 /ingest
# 关键：先 AT+CMGF=1 文本模式，再 AT+CNMI=2,1,0,0,0 开启新短信上报
```
适合长期挂机；注意模块供电与天线，`+CMTI` 上报比轮询省电。

### 方案 D 现成云转发
「短信转发宝」类 App / 自建 frp + 手机端脚本；只要是 HTTP POST 到 `/ingest` 即可。

### 起服务 + 防火墙
```powershell
python vmax_smsd.py --host 0.0.0.0 --port 8787 --secret s3cr3t --state sms_inbox/codes.jsonl

# 仅放行私有网络（公网不要开）
New-NetFirewallRule -DisplayName "vmax-smsd" -Direction Inbound -Protocol TCP `
  -LocalPort 8787 -Action Allow -Profile Private
```

> ⚠️ 无 `--secret` 时 daemon **拒绝**绑定非环回地址（防止整个局域网读到你的验证码）。
> `/latest` 现在也需要 secret。

---

## 2. 通道自检（2 分钟）

```powershell
python vmax_smsd.py --self-test        # 6 条中英文短信解析语料

# 一键验收：平台连通性 + 收件箱鉴权/语义 + 合成短信注入
python vmax_accept.py --smsd http://127.0.0.1:8787 --secret s3cr3t --smsd-selftest
```

期望最后一行：`ACCEPT_READY=True`

对着表格复核一遍（不联网）：`python vmax_accept.py --report accept.json` —— 期望 `ready  True`、`hard   (none)`、无 `fail`。

- [ ] `smsd_health / latest_auth / secret_enforced / code_semantics / ingest / readback` 全 OK
- [ ] 手机侧真发一条测试短信（或 `curl -X POST .../ingest -H "X-Sms-Secret: s3cr3t" -d "phone=<你的号>&text=【VobileMax】您的验证码为 482913"`）
- [ ] `curl "http://127.0.0.1:8787/latest?secret=s3cr3t"` 能看到 `482913`

> `--smsd-selftest` 会往 `13000000000` 写一条假短信，仅用于证明链路；不会发给任何人。

---

## 2.5 并发自检（10 秒，上量前必跑）

```powershell
python tests\test_batch_parallel.py
```

四个号同时跑、短信故意乱序到达；全 PASS 说明「不串码 + token 不重复发放 + 限流桶不丢更新 + 退出码正确」。
批量上量前跑一次，比事后查「为什么两个号用了同一个 token」便宜得多。

---

## 2.6 号源筛选（买号/换号时先跑）

```powershell
python vmax_probe.py --input candidates.csv --out probe.jsonl `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --captcha queue --queue-dir tokens --delay 3
```

- [ ] `probe.jsonl.summary.json` 里 `by_status.ok` 占比够高（先探 3–5 个再决定要不要买这批）
- [ ] `sms_timeout` 多 = 转发器没接上；`send_rejected` 多 = 号段不行

> 只发码不建号，不占写配额，也不会 429；验证码只以掩码形式落盘。

- [ ] `by_status.already_registered` 多 = 这批是老号/回收号（发码就被拒，不建号也不发短信）→ 别买

> 号码被拒在两处：`send-code` 直接说「已注册」（openapi 的原话是「向**未注册**手机号发送」），
> 或 `/register` 回 409（openapi 的**描述原文**是「注册唯一字段已被占用」，body 未实测）。
> 两种都会记成 `phone_taken`，续跑自动跳过。
> 老号不是废卡：`--on-taken reset` 或 `python vmax_reset.py --input results.jsonl` 能用密码重置把它
> 变成你自己的账号（1 token + 1 短信，第二步无滑块），见 RUNBOOK §3.6。

---

## 3. 平台侧彩排（2 分钟，不发短信）

```powershell
python vmax_accept.py --smsd http://127.0.0.1:8787 --secret s3cr3t --rehearse
```

期望：`rehearse_ready: READY=True` + `ACCEPT_READY=True`
（内部会解 2 个真滑块，用非法手机号免费探真伪，再把 `/register` 推到短信闸门）
- [ ] 跑过多轮后回头看趋势：`python vmax_accept.py --history-only "runs/accept-*.json"`
      → 看 `ready N/M runs`、`changed`（判决最后一次翻转）、`check changes`（哪一项 `fail -> ok` / `ok -> fail`）。
      对比的是检查状态不是文案，换机器或改文案都不会误报。

---

## 3.5 第一张卡：一条命令判定 READY（推荐，[R117]）

```powershell
python vmax_ready.py --input cards.csv `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --smsd http://127.0.0.1:8787 --secret s3cr3t --out runs\ready `
  --form-captcha device        # 表单不再花 token；发码仍 1 个（R140 实测）
```

只花第一张卡：**1 token（发码）+ 1 条真实短信 + 1 次 `/register` + 1 次登录**；`--form-captcha device`
让表单那一步 0 token（不加这个参数就是默认的 `auto`，表单可能再解一个）。
判据是从 `results.jsonl` / `logins.jsonl` 反读的，不看退出码：

- [ ] 最后一行 `verdict=ready exit=0`
- [ ] `runs\ready\READY.md` 六行全 `ok`：303 / `code=` / `state` 回显 / `access_token` / `uid` / 复验 ok
- [ ] `runs\ready\READY.md` 的**出处判据**两行也全 `ok`：`row_fresh`（不是日志恢复行）、`sms_this_run`（本轮真发过码）
- [ ] `runs\ready\ready.json` 的 `cost` 是 `1 / 1 / 1`
- [ ] `runs\ready\ready.json` 的 `run.results_before` 是 `0`（干净的 `--out`；不为 0 就是 `dirty_out`，换目录重跑）
- [ ] 失败时看退出码：`9` = 收码通道没起（0 token / 0 短信）；`1` = `READY.md` 里标 **FAIL** 的那一行（`stale_artifacts` = 本轮 0 行、盘上只有旧行）；`2` = 参数错或 `--out` 不干净；`3` = 写桶还在 429（等 ≥200 s 再跑）
- [ ] 判 READY 之后**不要再往同一个 `--out` 跑第二遍**：第一遍的字节会被覆盖成 `DIRTY_OUT`（这是有意的），要复查请复制 `ready.json` / `READY.md` 存档

通过后再走 §5 灰度。

- [ ] 想顺手把 PROTOCOL 剩下的问号关掉（每项 1 条短信起）：`python vmax_onecard.py xdevice --phone 138... --out runs\onecard\xdevice.json`（码能不能跨 session）、`python vmax_onecard.py ttl --phone 138... --low 60 --high 600 --rounds 3 --out runs\onecard\ttl.json`（码活多久）、`python vmax_onecard.py nosms --phone 138... --out runs\onecard\nosms.json`（不带 `sms_code` 提交会不会被收）。
      读法、产物与退出码见 RUNBOOK §3.7；不想花钱先加 `--dry-run` 看计划。

> 想先看会跑什么（不花钱、不建目录）：`python vmax_ready.py --input cards.csv --dry-run`

---

## 4. 单号实跑（5 分钟，唯一花钱的一步）

```powershell
# 终端 A：带背压的农场
python vmax_tokens.py --max-queue 2 --every 12 --workers 2 --duration 900

# 终端 B：1 个号
python vmax_accept.py --smsd http://127.0.0.1:8787 --secret s3cr3t `
  --phone 13xxxxxxxxx `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&timeout=180&since={since}" `
  --captcha queue --live --yes
```

成功判据（三条同时成立）：
- [ ] `live_account: ok`
- [ ] `accept_live.jsonl` 里 `status=ok` 且 `uid` 非空
- [ ] `token.expires_in` ≈ 7200

> `&since={since}` 让「重发短信」只认新码，避免拿旧码白烧一次尝试（单码只有 5 次机会）。

---

## 5. 灰度 5 号（5 分钟）

```powershell
python vmax_go.py --input accounts5.csv --out results.jsonl `
  --captcha queue --queue-dir tokens `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&timeout=180&since={since}" `
  --sms-resends 1 --max-fail 5 --max-minutes 60 --yes
```

- [ ] 5/5 成功，或失败项都能在下面的对照表里找到原因
- [ ] 没有触发 429；`python vmax_pacer.py status` 仍在窗口内
- [ ] 汇总里的 `taken=N` 是「已注册老号」，**不计入 `--max-fail`**；退出码 **7** = 没有真失败、只是跳过了老号
      （别当失败重跑，那只会再烧一遍 token）

---

## 5.5 复验 / 续期已有账号（不花短信）

JWT 只有 3600 s，过期不用重新注册 —— 账号密码登录能直接续：

```powershell
python vmax_login.py --input results.jsonl --out logins.jsonl `
  --captcha queue --queue-dir tokens --limit 5
```

- [ ] `ok=N/M`；`by_kind` 里 `auth_failed`（401）和 `no_credentials` 是终态，续跑不再重试
- [ ] 只有 `phone,password` 的行也能验：服务端接受「手机号当用户名」，结果会标 `username_used_phone`
- [ ] `captcha_tokens_spent` ≈ 成功的行数（**密码先于滑块校验**：401 不消费 token，会被下一行复用；
      看 `tokens_popped` / `tokens_reused` 两个计数对账）
- [ ] 撞 429（exit 3）就停 ≥200 s，别轮询

> 默认不落 `access_token`（活凭证），只存掩码；确实要落盘才加 `--save-token`。

---

## 6. 全量 + 长跑

```powershell
python vmax_tokens.py --max-queue 2 --every 12 --workers 4 --duration 3600   # 终端 A
python vmax_go.py --input accounts.csv --out results.jsonl ...               # 终端 B
```

- 断点续跑：同一个 `--out` 会自动跳过 `status=ok` 的号
- 429 出现即**全局停**：`vmax_go` 会中止并返回 3；等 ≥10 分钟（首封 600 s，探测会续封），期间不要探活
- 产能参考：单上下文 12–60 s/token；但**一个出口 IP 只有 ~30 写/h ⇒ ≈25 账号/h**（R156）；
  4 进程共享一个直连只会更快 429，要多 IP 才有线性收益
- 跑批前先对账：`python vmax_fleet.py --scan runs --check cards.csv --write-fresh runs\fresh.csv`（已 ok / 今天已 taken 的号 0 token 0 短信跳过；RUNBOOK §4.1）
- 用付费面板买号时：`--count` 是目标（重启只补差额），`--out` 必填才有续跑与 `<out>.summary.json`；面板看门狗 `--panel-recheck` 默认 5 单 / 300 s，探针失败 exit 9 且不再买（RUNBOOK §3.1.5）
- 想一条命令跑完（预检 → 农场 → 批量 → 复验 → 汇总 + `manifest.json`）：`python vmax_day.py --input cards.csv --sms "..." --out runs\1026\results.jsonl --yes`（RUNBOOK §4.0；跑批看门狗默认每 5 个号 / 300 s 复探，可用 `--sms-recheck` / `--sms-recheck-seconds` 调，0 = 关）

---

## 7. 失败对照表

| 现象 | 真实原因 | 处置 |
|---|---|---|
| `手机号格式不正确` | 传给 `/register` 的不是 11 位（daemon 会剥 +86，但别传 12 位以上） | 检查 CSV 号列 |
| `该手机号未通过运营商验证` | 170/171 虚商段、空号、停机 | 换实卡 |
| `503 手机号运营商验证暂不可用` | 号码级上游不可用（`13800138000` 恒 503） | 换号，不是限流 |
| `503 注册奖励服务暂不可用` | 服务级抖动（码未核销、没建号） | 已内置 `--retry-503 1 --wait-503 10` 自动重提同一码；仍失败就等一会儿重跑，**不要重发短信** |
| `请先获取短信验证码` | send-code 没成功 / 被滑块拦 / 被限流 | 看 `send_code` 字段，重解 token |
| `验证码已过期` | 码超过 5 分钟或已被核销 | 重发（60 s 冷却），确认 `since` 生效 |
| `验证码不正确` | 抓到上一条旧码 / 抄错 | 确认 URL 带 `&since={since}`（现在是亚秒精度，截断成整数会在同一秒里串旧码） |
| `Captcha verification rejected` | token 已用掉或 >90–120 s | 提高农场并发/缩短队列深度 |
| `429` | 写接口 IP 限流 | 立即停 ≥200 s，**不要轮询**（会续封） |
| `live_account: fail` 但注册成功 | `oauth/token` 交换失败（PKCE/state 不匹配） | 检查 `--redirect` 与 client |
| `phone_taken` / 批量 exit 7 | 这个号以前注册过（发码阶段就被拒） | 正常，换号；同一 `--out` 续跑会自动跳过 |
| 续跑把这个号判成 `phone_taken`，但上次明明看到过 303 | 上次进程在写 `results.jsonl` 之前就死了 | 看 `<out>.journal`：有 `uid` 的行会在启动时被免费恢复（`via:"journal"`）；没有 uid 的走 `--on-taken reset`（1 条短信）。归档时日志和 results 一起留 |
| `vmax_login` 401 `auth_failed` | 用户名/密码不对（**密码先校验，不消费 token**，留给下一行复用） | 终态，别重试烧 token |
| `vmax_login` 403 `captcha_required` | 滑块没过 / token 已用掉 | 换新 token 重试（可续跑） |

---

## 8. 红线

- `--secret` 必设；8787 只开私有网络，不要映射公网
- 一号一账号；不要用他人号码或接码平台的「共享号」
- 号码与账号行为要像真人：单 IP 突发 7 次、持续 ~30 写/h 是硬上限，别压缩间隔
- [ ] `<out>.journal` 和 `results.jsonl` 一起归档（崩溃后免费认回账号的唯一依据）
- [ ] `<out>.lock` 是运行期草稿（归档可删）；`<out>.journal` 必须留着
- 证据留档：`accept.json` / `accept_live.jsonl` / `results.jsonl` 一起归档
