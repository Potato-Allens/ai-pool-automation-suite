# 真实批量上线 Runbook（auth.vobilemax.cn）

> 目标：把「协议已验证」推进到「真实号批量建号」。
> 前置：能收短信的 +86 实卡/接码号源、可用出口、滑块方案（农场 / assist / 打码平台）。

---

## 0. 上线前检查清单

- [ ] 环境：Python ≥ 3.10 + `pip install -r requirements.txt`（**只有 `requests` 是必装的**；`playwright` / `pyserial` / `numpy+pillow+scipy` 只在滑块/模块/图像求解时用得到，R129 实测）
- [ ] 号源：能收到验证码，且不是 170/171 虚拟段
- [ ] 出口：**注册写接口按出口 IP 限流**（突发 ~7 次；持续 ~30 写/h；撞 429 首封 ≥10 分钟，探测会续封）；并发只能靠多出口 IP，不能靠多线程
- [ ] 节流：`vmax_pacer.py` 默认 **5 次/600 s（30 写/h）** + 指数退避（600 s 起、7200 s 封顶）；`python vmax_pacer.py status` 先看是否在封禁期
- [ ] 滑块：`vmax_tokens.py` 农场或 `assist` 人工；打码平台走 `--captcha param`
- [ ] 彩排：`python vmax_go.py --rehearse` 必须 `READY=True`（不发短信）；单上下文 ~1 token/40–60s，要开 4–6 个才能吃满限流额度
- [ ] 接码：`http:` / `cmd:` / `file` 三选一，先单号验证
- [ ] 时钟：与本机偏差 < 2 分钟（`vmax_doctor.py` 会报）
- [ ] 合规：号码来源、短信费用、站点条款

## 1. 预检

```bash
python vmax_doctor.py \
  --base https://auth.vobilemax.cn \
  --queue-dir tokens \
  --proxy http://user:pass@host:8080      # 可选
```

测试环境加：

```bash
python vmax_doctor.py --base https://test-auth.vobilemax.cn \
  --redirect https://test-dreammaker.vobilemax.cn/auth/callback
```

必须全部 `[OK]`（`egress`/`tokens` 不阻塞）。关注：

| 检查 | 期望 | 异常含义 |
|---|---|---|
| `openapi` | 41 paths | 站点改版/被墙 |
| `agreements` | 12:terms, 13:privacy（test 为 11/12） | 协议 id 变了，客户端会自动跟随 |
| `captcha` | `8vo785 / leukcmsx` | 验证码租户变更，需重抓 |
| `clock` | 偏差 < 120s | 偏差过大会影响滑块 |
| `ready` | `/health/ready` → `ready` | 依赖（库 / 短信上游）没就绪；此时发码会集体失败 |
| `egress` | 代理出口 IP | 与直连相同 = 代理没生效 |


`--register-gate` 会额外发一次**空验证码**的 `/register` 表单（不发短信、不建号），用服务端答复确认
「滑块闸门仍开、只缺短信码」：

```powershell
python vmax_doctor.py --register-gate --flows --skip-egress
python vmax_doctor.py --base https://test-auth.vobilemax.cn `
  --redirect https://test-dreammaker.vobilemax.cn/auth/callback --register-gate
```

判定：`请先获取短信验证码` / `验证码不正确` / `已过期` → PASS；`503 手机号运营商验证暂不可用` →
该号被上游拒绝（**换号重试**，不是整站故障）；出现 200/303 → 立即停下排查。

两道门之后再加一道**契约门**（免费、不联网写入）：把今天的协议面存成基线，每次开跑前比一次。
站点改了字段名（比如 `sms_code` → `code`）时，`doctor` 仍然全绿，而跑批会“每个号都报错”。

```powershell
# 第一次（或每次确认改版后）：存基线
python vmax_contract.py snapshot --out contract.json

# 之后每次跑批前：比对（0 = 一致，1 = 关键漂移，2 = 站点没答）
python vmax_contract.py check --baseline contract.json

# 想把“新增端点 / 协议版本变”也当成阻断：
python vmax_contract.py check --baseline contract.json --strict
```

| 结论 | 含义 | 怎么办 |
|---|---|---|
| `[CRIT]` | 我们调用的东西变了（路径没了 / 字段没了 / 类型变了 / 新增必填 / scene 换了 / agreements id 没了 / login 表单少输入） | **先不要花钱**，按提示改客户端或重捕验证码 |
| `[WARN]` | 新增端点 / 新增字段 / 协议版本升级 / 新加载脚本 | 可以跑；看一眼是不是新增了需要提交的字段 |
| exit `2` | health/openapi 都没答 | 网络/域名问题，不是协议问题 |

建议基线跟输出一起留档：`runs\1026\contract.json`，以后回看“那一天站点长什么样”。

## 2. 令牌农场（浏览器与 HTTP 解耦）

**先记住两个数**：token 只活 **90–120 s**（§3.8），且用一次就废。所以农场必须**带背压、边产边用**，
不能「先解 100 个再跑」。

推荐用法（背压 + 全局限速，跑一小时）：

```powershell
python vmax_tokens.py --max-queue 2 --every 12 --workers 4 --duration 3600 --out-dir tokens
```

| 参数 | 含义 |
|---|---|
| `--max-queue 2` | 队列里已有 2 个未消费 token 就暂停生产（关键） |
| `--every 12` | 所有 worker 合计每 12 s 最多产 1 个（= 5/min，正好对上 §3.4 的写限流） |
| `--duration 3600` | 跑 1 小时；也可用 `--n N` 指定总数 |
| `--stale-after 120` | 启动时把超过 120 s 的旧 token 改名 `.stale`（它们已经不可能有效） |
| `--fail-cooldown 20` | 单个 token 失败后睡 `20×连续失败数` 秒（封顶 300 s），坏时段不空转烧 proxy；`0` 关闭 |
| `--max-consec-fail 8` | 连续 8 个 token 全败就退出该 worker（阿里云坏时段实测出现过 6 连败）；`0` 永不放弃 |

要点：

- 单 worker 约 20~60 s / token（单次 35%，6 次重试约 92%）
- 4 workers ≈ 4 倍解算吞吐，但**写队列被 `--every` 全局限速**，不会产出用不完的货
- 消费端 `pop_token()` 也会丢弃 >90 s 的 token（`VMAX_TOKEN_MAX_AGE` 可调），双保险
- 队列文件 `tokens/tok_*.json`，消费即改名 `.used`；`.stale` 可随时删
- 消费端 `pop_token()` 在队列空时会**阻塞等待**（`vmax_batch --queue-wait 600`，`vmax_go` 同参数），
  农场慢于消费时不会判失败；取号在 `mkdir` 互斥里「先读内容、再改名 `.used`」（原因见 §2.1，
  **不要**改回「靠 rename 判定归属」）

### 2.1 队列取号为什么必须加锁（R95 实测，2026-10-26）

原本的写法是「rename 成功 = 我拿到这个 token」，理由很自然：同一个源文件只能被改名一次。
在 Windows 上这个假设不成立，实测两种错法都很贵：

| 写法 | 8 线程 / 200 token 的结果 |
|---|---|
| rename 后读改名后的文件 | 一度把 **124–163 个 token 当读失败丢掉**（rename 刚返回时 `open()` 有时看不到新名字） |
| 先读内容、再 rename（无锁） | 200 个 token **交付 292–309 次，重复 92–109 次**（首次 rename 尚未提交，后续 rename 仍会成功） |

后果：token 是一次性的（送码用 1 个、`/register` 再用 1 个），重复交付 = 有一路必然 401/「验证码已使用」；
被丢掉的 = 白烧一次解算（≈3 次滑块，单 token 20–60 s）。小批量时表现为「4 个号消耗了 5 个 token」。

现在的写法：`_queue_lock()`（`os.mkdir` 原子创建目录，跨线程+跨进程）包住「列出 → 读内容 → 改名 `.used`」。

- 锁内只做几微秒的活；200 次取号 0.2 s → 约 1.2 s（≈6 ms/次），按 30 账号/h 无感。
- 内容读不出来（半截写入 / 空 `param`）时**不动这个文件**，留给人工或下次重试，不再「领取即烧」。
- 持锁进程崩了会留下 `.pop.lock` 目录：超过 20 s 视为死锁自动接管，正常情况永远不会触发。
- 回归：`tests/test_pop_token.py`（22 项）覆盖 8 线程 / 2 进程 / 坏 JSON / 空 param / `.stale` / `--queue-wait` 等待 / 可选 `VMAX_POP_LOG`。

## 3. 接码接入

| 方式 | 参数 | 适用 |
|---|---|---|
| 控制台手输 | `--sms prompt` | 单号验证 |
| 文件轮询 | `--sms file --sms-dir inbox` | 接收端写 `inbox/<phone>.txt` |
| 命令 | `--sms "cmd:python get_sms.py --phone {phone}"` | 自有短信网关 |
| HTTP | `--sms "http:https://api.example/code?phone={phone}"` | 接码平台 API |

`http:` 适配器会先找 JSON 里的 `code`/`sms_code`/`verify_code` 等键，找不到再在全文抓 6 位数字。
返回示例（任一即可）：

```json
{"code": "123456"}
{"data": {"sms_code": "123456"}}
```

> **先看这条**：5sim 不卖中国号（`guest/countries` 无 `china`），onlinesim 中国免费号为空，
> 而各面板的中国库存多为 170/171 虚拟段——本站 `/register` 对 170/171 直接判「未通过运营商验证」。
> 因此**面板买号对本目标大概率无效**；首选实卡池（§3.2）。面板只作补充，且先用 `--allow-virtual` 单号试。


### 3.1 付费接码平台（推荐用于批量）

`vmax_panel.py` 把「买号」和「注册」串起来：面板给号 → 发码 → 轮询面板取码 → `/register` → token。

```powershell
# 摸清面板支持的国家/服务（不花钱）
python vmax_panel.py --provider sms-activate --key $env:SMS_PANEL_KEY --list-countries
python vmax_panel.py --provider sms-activate --key $env:SMS_PANEL_KEY --list-services

# 试 1 个号（价格护栏 0.5 元，超出直接取消订单）
python vmax_panel.py --provider sms-activate --key $env:SMS_PANEL_KEY `
  --country china --service <service-code> --count 1 --max-price 0.5 `
  --captcha queue --queue-dir tokens --out results.jsonl

# 5sim（Bearer token）
python vmax_panel.py --provider 5sim --key $env:SMS_PANEL_KEY `
  --country china --product <product-code> --count 3 --max-price 0.8
```

要点：

1. **号码是面板给的**，所以不要在 CSV 里预置号；`--count` 是**目标**（`--out` 里已 ok 的行算数，见 §3.1.5）。
2. `--max-price` 是硬护栏：超过报价立刻 `setStatus 8` 退单。
3. 取码超时默认 180s（`--sms-timeout`），成功后 `setStatus 6` 完结订单，不会重复计费。
4. 与 `vmax_batch.py` 的分工：**自己的卡池**用 batch；**面板买号**用 panel。
5. `--dry-run --dry-phone 13800138000 --dry-code 123456` 可用 mock 面板跑通编排（仍会真发短信，仅用于自测）。
6. 面板号大多是虚拟号段，`/register/send-code` 前的运营商在验可能直接拒（`--allow-virtual` 仅跳过本地预判，服务端该拒还是拒）；买号前先确认该服务支持 +86 实卡。
7. **`--form-captcha device` 省的是「表单」那一半**（R146）：发码仍要 1 个 token，表单改用站点自己的
   `signup-check` 路径（`GET /login` → 32 位小写 hex `auth_fingerprint` → `POST /register` 交空
   `captcha_verify_param`）。一卡成本从 **2 token 降到 1 token + 1 SMS**；任何一步被拒都会自动退回
   付费 token（行里 `form_captcha=device_fallback`），不会因为省 token 丢卡。默认仍是 `auto`，
   老命令行的含义不变；`<out>.summary.json` 的 `form_captcha` / `form_device` / `form_reuse` /
   `form_fallback` / `form_extra_tokens` 告诉你这次到底有多少张卡真走了设备路径、多少张蹭了
   send-code 的 token、多少张又买了一个。

### 3.1.0 先跑 `vmax_ready.py`：一张卡 + 六条链路判据 + 两条出处判据 [R117/R139]

```powershell
python vmax_ready.py --input cards.csv `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --smsd http://127.0.0.1:8787 --secret s3cr3t --out runs\ready `
  --contract contract.json
```

顺序：`channel_check`（0 token，挂了直接 exit 9）→ `vmax_accept --smsd-selftest`（平台 / 收件箱 / 模块态
+ 一条合成短信）→ `vmax_day` **只跑第一张卡**（1 token + 1 条真实短信 + 1 次 `/register` + 1 次登录）→
从 `results.jsonl` / `logins.jsonl` 反读判定。产物 `runs\ready\ready.json`（机器）与 `runs\ready\READY.md`（可直接贴的表）。
账在哪看（R151）：`ready.json` 的 `form_captcha` 是这张卡实际走的路径（`device` / `reuse` 免费，`fresh` / `*_retry` / `device_fallback` 多买一个），`cost.captcha_tokens` = 1 + `cost.form_extra_tokens`；
软判据 `captcha_reused` 就是「这一卡的表单没有第二次解」。默认 `auto` 的正常结果是 `reuse` + 1 token，不是 2。

`--contract contract.json`（R126）：这条命令会把基线一路交给内部的 `vmax_day`，所以契约关键漂移会在**第一张卡、第一条短信之前** exit 2；单卡实验 `vmax_onecard` 则是自己比、拒跑时 exit 4（见 §3.7）。

六条链路判据（这一行看起来像成功）：

| # | 判据 | 证据 |
|---|---|---|
| 1 | `register_303` | `register.status == 303` |
| 2 | `location_code` | `Location` 里有 `code=` |
| 3 | `location_state` | `Location` 里有 `state=`（回显） |
| 4 | `access_token` | `/api/auth/oauth/token` 回 `access_token` + `token_type` |
| 5 | `uid` | `user.uid` 非空 |
| 6 | `login_ok` | `vmax_login` 复验 `status=ok` |

两条出处判据（这一行是**本轮真短信**换来的）——R139 加，缺一条都不算 READY：

| 判据 | 证据 | 为什么 |
|---|---|---|
| `row_fresh` | 行里没有 `via:"journal"` / `journal_recovered` | 日志恢复行是上一轮崩溃的残骸（0 短信、无 token），不是这次验收 |
| `sms_this_run` | 行里 `send_code.ok == true` | 没发过码的行不能证明「载体把码送到了这张 SIM」 |

出处不是洁癖：R139 实测过——**同一个 `--out` 再跑一次**，`vmax_day` 发现号都做完了、退出 4，
而盘上上一轮的 303 / token / uid / 登录行还在，旧版 `vmax_ready` 照样打 **READY（0 短信 / 0 token）**。
现在的规矩是：开跑前记下行数偏移，只判**本轮新增**的行；目录里已有旧行就 **exit 2 `dirty_out`**
（`READY.md` 会被改写成 DIRTY_OUT，盖掉那个过期的 READY），真要继续得显式 `--resume`，
而 `--resume` 下仍然过不了 `row_fresh` / `sms_this_run`。

退出码：0 READY（八条判据全过）、1 判据未过 / 本轮 0 行（`stale_artifacts`；`READY.md` 里那一格是
**FAIL**，`ready.json` 的 `checks[]` / `provenance[]` 有 detail）、2 参数错或 `--out` 不干净
（`dirty_out`）、3 写桶还在 429（等 ≥200 s）、9 收码通道没起（**0 token / 0 短信**）。
`--dry-run` 只打印三条命令且密钥打码；`--no-accept` / `--no-sms-check` 分别跳过第二、第一步；
`--resume` 允许不干净的 `--out`，但只把本轮新增的行当证据。

### 3.1.1 一条命令验收（实卡到货当天必跑）

```powershell
python vmax_accept.py --smsd http://127.0.0.1:8787 --secret s3cr3t --smsd-selftest   # 4 s
python vmax_accept.py --smsd http://127.0.0.1:8787 --secret s3cr3t --rehearse        # +24~45 s
python vmax_accept.py --smsd ... --phone 13xxxxxxxxx `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&timeout=180&since={since}" `
  --captcha queue --live --yes                                                        # 1 real account
```

判据：最后一行 `ACCEPT_READY=True`，且 `accept.json` 里 `hard_failures: []`。
看清账：`accept.json` 的 `live` 段现在带 `form_captcha` / `form_extra_tokens` / `device_ok`，
并多一条**软**检查 `live_form_cost`（`hard=False`）—— 它说这张卡的表单是白嫖（`device` /
`reuse`，0 个）还是又买了一个（`fresh` / `auto_retry` / `device_fallback` / `device_retry`，1 个）。
软行不改变 `ready`：花了钱不等于门没过，但你得知道钱花在哪。
注意 `smsd_modem` 这条 [R116]：daemon 活着不等于能收码 —— `vmax_smsd --modem COM3` 拔了模块/卡被 PIN 锁住时
`/health` 仍然 `ok=true`，所以 `settings` 里有 `modems[]` 就一定要求至少一个 `connected=true`，否则 `ACCEPT_READY=False`；
只有**没配** `--modem`（纯手机转发 / 文件投递模式）时这一条才是软过。
跑完随时用 `--report` 复核（不联网、只读文件，30 秒内看完整张表）：

```powershell
python vmax_accept.py --report accept.json      # ready=True -> exit 0，否则 exit 1，读不到 -> exit 2
```

表格里 `st` 四态：`ok` 硬检查通过 · `fail` 硬检查失败（挡住上线）· `warn` 软检查失败（提示但放行）· `skip` 软检查跳过。
`summary` 行给出四态计数；有 `fail` 或 `warn` 时不会打 `ALL GOOD`。文件超过 1 h 会提示「re-run the gate」。
`--smsd-selftest` 会 POST 一条合成短信到 `13000000000` 证明手机→PC→工具链路。
完整 30 分钟流程见 [`CHECKLIST.md`](CHECKLIST.md)。

收码端的三个新约束（2026-10-26）：

- `&since={since}`：只认本次 send-code 之后到达的码，重发时不会拿旧码白烧一次尝试
- `--code-ttl`（默认 300 s）：超过平台码有效期（5 min）的码自动作废
- 非环回绑定必须带 `--secret`，否则 daemon 直接拒启；`/latest` 同样要 secret

### 3.1.2 号源探针：买号前先筛（每个号只花一条短信）[推荐]

```powershell
python vmax_probe.py --input candidates.csv --out probe.jsonl `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --captcha queue --queue-dir tokens --sms-timeout 180 --workers 1 --delay 3
```

它只做 `POST /register/send-code` + 等短信，**绝不调用 `POST /register`**，所以：

| 不会发生 | 原因 |
|---|---|
| 429 | 写接口配额一次都不占 |
| 建出账号 | 没有提交表单 |
| 表单滑块被消耗 | 每号只用 1 个 token（送码那次） |
| 验证码进日志 | 输出只留 `code_len` + `code_masked`（如 `1****6`） |

输出 `probe.jsonl` 每行一个号，`status` 取值：

| status | 含义 | 处置 |
|---|---|---|
| `ok` | 过运营商校验 + 真收到码（带 `sms_seconds`） | 可用 |
| `send_rejected` | 服务端拒发（如「该号码暂不支持注册」/ 运营商在验不过） | 换号 |
| `sms_timeout` | 码没到（`--sms-timeout` 内） | 号不可用 / 转发器没接上 |
| `virtual_range` | 170/171 虚商段，本地直接拦（`--allow-virtual` 可强制试） | 基本没戏 |
| `bad_format` | 不是 11 位大陆号 | 修输入 |
| `no_token` | 队列空且 `--queue-wait` 内没补货 | 先把农场跑起来 |

批量买号时先探 3–5 个，确认这批货的号段能过再上量；`probe.jsonl.summary.json` 里有
`by_status` 分布，可以直接判断这批号值不值得买。

### 3.1.3 验收历史：多轮 `accept.json` 横向对比 [离线]

单份 `accept.json` 只说明「刚才那一刻」。真正要回答的是「什么时候坏掉的、坏的是哪一项」，
所以闸门每跑一次就另存一个文件，再让工具自己对比：

```powershell
# 每轮跑完另存一份（建议带日期）
python vmax_accept.py --smsd http://127.0.0.1:8787 --secret s3cr3t --out runs/accept-1031.json

# 只看历史（不跑检查、不联网）
python vmax_accept.py --history-only "runs/accept-*.json"

# 先看当前这份，再看历史
python vmax_accept.py --report accept.json --history "runs/accept-*.json"
```

输出长这样（下面这份就是仓库里三份真实报告的对比）：

```
ACCEPT HISTORY  3 run(s)
  ------------------------------------------------------------
  when         source                 ready  fail  warn  chg
  10-26 05:51  accept.json            yes       0     1  .
  10-26 05:53  accept_rehearse.json   yes       0     1  =
  10-26 07:15  accept_live_test.json  yes       0     0  =

  ready    3/3 runs
  latest   2026-10-26 07:15:58  (age 3m)
  changed  (verdict stable across the runs shown)
```

读法：

| 列 / 行 | 含义 |
|---|---|
| `ready` 列 | `yes` / `NO`；`NO` = 那轮有不通过的硬检查 |
| `fail` / `warn` | 硬失败 / 软告警（`hard=False` 的检查不通过记 warn，不算失败） |
| `chg` | 相对上一轮失败集合的变化：`+2` 新增 2 项失败、`-1` 修好 1 项、`=` 无变化、`.` 第一轮 |
| `changed` | 判决（ready 与否）**最后一次翻转**发生在哪一轮 + 那一轮的文件名 |
| `check changes` | 逐条 `check: fail -> ok` / `ok -> fail`，最多 10 条（更多写 `older change(s) not shown`） |
| `skipped` | 解析不了的文件（半截写入 / 非 accept 报告）只列出并跳过，**不会**让整张表消失 |

口径与坑：

- 只按报告里的 `ts` 排序，**不信文件名**；`ts` 相同再按路径兜底。从别的机器拷来的历史也能正确排序。
- `--history-limit N` 只保留最新 N 轮（默认 20，`0` = 全要）。
- 对比的是**每项检查的通过状态**，不是 `detail` 文案 —— 改文案不会误报「有变化」。
- 退出码：`--history-only` 找到 ≥1 份 → 0、一份都没有 → 2；带 `--report` 时以报告的 0/1/2 为准。

### 3.1.4 已注册号码（`phone_taken`）：在发码这一步就省下一条短信 [R96]

`POST /register/send-code` 的 openapi 描述是「向**未注册**手机号发送 register 用途验证码」，
所以已经建过号的号码会在**发码**时就被拒；`POST /api/auth/register` 另有 409，openapi 给它的
**描述原文**是「注册唯一字段已被占用」（response body 至今未实测）。两种都判成 `phone_taken`，
含义只有一个：「这个号已经有账号了」。

| 出现在 | 状态 / 字段 | 花多少 | 说明 |
|---|---|---|---|
| `vmax_probe.py` | `status=already_registered` | 1 个 token，**0 条短信** | 号源里混进老号时一眼看得出来 |
| `vmax_batch.py` / `vmax_go.py` | `register_error_kind=phone_taken` | 1 个 token，0 条短信、0 次 `/register` | 照常写进 `results.jsonl`，汇总里 `taken=N` |
| 断点续跑 | 直接跳过 | 0 | `load_skipped()` 只跳 `phone_taken` |

- **R104**：分类发生在 `vmax_register` 内部（`RegisterError` 带 `kind` + 部分结果），不是事后猜异常文字；
  被拒的那一行照样带着服务端原话（`send_code`）、`captcha_seconds` 和 `register_http_status`，
  所以单号（`vmax_register`）、批量（`vmax_batch`/`vmax_go`）、当天编排（`vmax_day`）三层对
  `phone_taken` / `rate_limited` 的判断完全一致。

判据（`vmax_register.is_phone_taken`）刻意保守：`已注册 / 已被注册 / 已存在 / 已被使用 / 已绑定`
才算；出现「未注册」的句子（例如 `手机号未注册`）**优先排除**。宁可把一个怪响应当普通失败，
也不要误跳一个能注册的好号。

退出码 **7** = 「没有真失败，但有号被跳过」：

```powershell
python vmax_batch.py --input accounts.csv --out results.jsonl ...
# [batch] done in 41s: ok=8 fail=0 taken=2 aborted=False   -> exit 7
# [batch] 2 phone(s) already registered elsewhere -> skipped (no token, no SMS)
```

看结果用 `python vmax_report.py results.jsonl`：`taken` 单独一列，成功率按 `ok / (ok+error)` 算
（被跳过的老号根本没发起注册，不该拉低成功率），`top errors` 里也不会再出现「该手机号已注册」。

- 和 429（3）/ `--max-fail`（5）/ `--max-minutes`（6）分开，是为了让 CI 一眼区分
  「这批号里有一部分是老号，其余是好的」和「真挂了」——别把 exit 7 当失败重跑再烧一遍 token。
- `phone_taken` **不计入** `--max-fail`：回收号批次里混几个老号很正常，不能因此把整批拉闸。
- 全部被跳过也是 7（不是 4）：4 表示「都成功过」，7 表示「都跳过」，补给新号才是下一步。
- 老号不是废卡：`--on-taken reset` / `vmax_reset.py` 能用密码重置把它变成你自己的账号（§3.6）。

### 3.1.5 面板批量的钱怎么不亏（目标制 / 逐行落盘 / 看门狗）[R120]

面板按号计费，所以 `vmax_panel` 的批量语义跟 `vmax_go` 对齐（钱的颜色一样）：

```powershell
python vmax_panel.py --provider sms-activate --key $env:SMS_PANEL_KEY `
  --country china --service <svc> --count 100 --max-price 0.5 `
  --out runs\panel\results.jsonl --max-fail 5 --panel-recheck 5 `
  --form-captcha device
```

1. **`--count 100` 是目标，不是「再买 100 个」**：`--out` 里已经 `status=ok` 的行算进目标。
   跑到一半崩溃，重启同一条命令只补差额，不会重复买。
2. **每收到一行就落盘**：崩溃不会丢掉已经付过钱的账号；`<out>.summary.json` 记
   `bought / ok / fail / taken / panel_dupes / probes / error_kinds` 与停止原因。
3. **429 立刻停**（exit 3），不会把下一个号买进封禁窗口；`--max-fail N` 与
   `--max-minutes` 同样在买下一个号之前生效。
4. **面板看门狗**：每 `--panel-recheck` 次购买（默认 5）或每 `--panel-recheck-seconds`
   秒（默认 300）免费探一次 `balance()`；探针失败就停在下一单之前（exit 9），
   从那一刻起 0 支出。`0` = 关掉该类触发。
5. 面板连续给同一个号 → 立刻退单、不重复注册（汇总里 `panel_dupes` 计数）。
6. **token 的钱也要看**（R146 / R150）：`--form-captcha device` 时每卡 1 个 token（只有发码），
   默认 `auto` 通常 1 个（复用发码那个 token，行里记 `reuse`），首投被拒才 2 个。汇总里
   `form_captcha` 是本次策略；`form_device` / `form_reuse` 是白嫖的行数（0 个表单 token）；
   `form_extra_tokens` 是为表单又买了一个的 token 数（`fresh` / `auto_retry` / `device_fallback` /
   `device_retry`，`form_fallback` 是其中「设备被拒」那一半）。不变式：
   `form_device + form_reuse + form_extra_tokens == 本轮落盘行数`；`none` 桶只应出现在 R150 前的
   老记录里 —— 只要 `form_extra_tokens > 0`，就别按 1 token/卡 做预算。

退出码：0 达标、1 有账号失败、3 429 中止、4 无事可做、5 `--max-fail`、
6 `--max-minutes`、7 买到的号全是已注册（号池脏）、9 面板挂了（acquire 失败或探针失败）。

**仍要提醒**：面板中国库存多为 170/171 虚拟段，本站 `/register` 会判「未通过运营商验证」——
这里解决的是「买了之后怎么不亏」，不解决「这个面板有没有能过运营商校验的号」。
真上量前先用 `vmax_probe.py` 或单号 `--count 1` 验证该面板的 +86 库存。

### 3.2 自建收码服务（实卡池，推荐）

一张能收短信的卡 + 一台常开的机器，就能把「实卡池」变成 HTTP 号源：手机端把短信转发到本机，
`vmax_register` / `vmax_batch` 用 `--sms http:` 取码。零第三方依赖（只用标准库）。

```powershell
# 收码服务（常开；建议只绑内网，务必带 secret）
python vmax_smsd.py --host 0.0.0.0 --port 8787 --secret s3cr3t --state sms_inbox/codes.jsonl

# 自检：解析规则（中英文短信、避免把手机号当验证码）
python vmax_smsd.py --self-test

# 模拟手机转发（换成你的短信内容）
curl.exe -X POST http://127.0.0.1:8787/ingest -H "X-Sms-Secret: s3cr3t" `
  -d "phone=13800138000&text=【VobileMax】您的验证码为 482913，5分钟内有效。"

# 取码（长轮询，最多等 timeout 秒）
curl.exe "http://127.0.0.1:8787/code?phone=13800138000&secret=s3cr3t&timeout=120"

# 接进注册链路
python vmax_register.py --phone 13800138000 `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&timeout=180"
```

手机侧转发（任选）：

| 端 | 做法 |
|---|---|
| Android | SMS Forwarder / Tasker：触发「收到短信」→ HTTP POST `http://<PC内网IP>:8787/ingest`，Header `X-Sms-Secret`，Body 表单 `phone=%from%&text=%text%` |
| 备用机脚本 | Termux + `termux-sms-list` 轮询，命中新短信即 POST 同款 body |
| 短信猫 / 网关 | 用下面的 `--modem` 直连（推荐）；老网关仍可 POST `/ingest` 或写文件后用 `--sms file` |

#### 3.2.1 短信猫 / 4G 模块直连（`--modem`，R113）

把卡插进 USB 上网卡/短信猫（EC20、ML307、SIM800、SIM7600 都行），一条命令收码，不用手机、不用转发 App：

```powershell
# 先看卡和模块是否正常：型号 / SIM / 信号 / 运营商 / 本机号码
python vmax_modem.py --probe COM3
python vmax_modem.py --probe tcp://192.168.1.50:2000     # ser2net / ESP-Link

# 常开收码（PDU 模式；开机先扫存量短信，断线自动重连）
python vmax_smsd.py --host 0.0.0.0 --port 8787 --secret s3cr3t `
  --state sms_inbox/codes.jsonl --modem COM3
```

| 参数 | 说明 |
|---|---|
| `--modem COM3` | 串口（需要 `pip install pyserial`）；也可写 `serial://COM3` |
| `--modem tcp://host:port` | 模块自带 TCP / ser2net / ESP-Link，零第三方依赖 |
| `--modem virtual` | 虚拟模块，演示/自测用：`POST /virtual/sms?secret=...` 注入一条短信 |
| `--modem-baud` | 默认 115200（EC20 常见 115200，SIM800 常见 9600） |
| `--modem-mode text` | 兜底文本模式；中文短信请用默认 `pdu`（text 模式受模块字符集影响） |
| `--modem-keep` | 收完不删 SIM 槽位（默认删除，避免存储满） |
| `--modem-probe` | 只打印型号/SIM/信号/运营商/号码并退出 |

要点：

1. **PDU 解码覆盖运营商实际下发形态**：GSM 7-bit（含转义表）、UCS2 中文、8-bit、字母发件人、UDH 长短信拼接。
   无硬件可验：`python vmax_modem.py --self-test`。
2. **开机先补收**：进程重启/拔插期间到的短信，连接后 `AT+CMGL` 扫一遍再按需删除，不会丢。
3. **断线不终结跑批**：任何 AT 异常 → 关闭传输 → 1/2/4…最多 30 s 重连；`/health` 的 `modems[]` 能看到
   `received / errors / reconnects / last_error`。
4. **去重**：`(号码, 正文, 时间戳)` 相同只交付一次（扫库 + 迟到的 `+CMTI` 不会重复）。
5. **PIN 锁卡直接报错**：`AT+CPIN?` 不是 `READY` 时连接失败并打印 `SIM not ready: +CPIN: SIM PIN`，
   不会出现「跑了一小时一条码都没进来」。
6. 常见故障：`+CSQ: 0,99` = 没信号；`AT+CMGF=0` 回 `ERROR` = 模块不支持 PDU（换模块或用 `--modem-mode text`）；
   `tcp://` 连不上 = ser2net 端口/波特率不对；收到 `+CMS ERROR: 321` = 槽位号失效（重启模块即可）。

接口一览：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ingest`（别名 `/sms`、`/webhook`） | JSON 或表单，字段 `phone`/`text`；`X-Sms-Secret` 或 body `secret` |
| GET | `/code?phone=&secret=&since=&timeout=` | 长轮询取码；无码返回 `404 {"code":null}`，`HttpSms` 会继续轮询 |
| GET | `/latest?limit=20` | 最近若干条（排障用） |
| GET | `/health` | 存活 + 计数 |

注意：

1. 号码统一转 11 位（自动剥离 `+86` / `86`），与 `vmax_register.normalize_phone` 一致。
2. 取码优先匹配「验证码 / 校验码 / 动态码 / verification code」附近的 6 位数字，并先把正文里的 11 位手机号抹掉，避免抓错。
3. `--state` 落 JSONL，进程重启不丢最近 500 条。
4. 绑 `0.0.0.0` 时**必须**带 `--secret`，否则同网段任何人都能读你的码。
5. `since` 自带**亚秒精度**（`{since}` 渲染成 `1792971942.123`），daemon 端的时间戳也是小数。
   用 `int()` 截断会出事：同一个号在同一秒里先探测再批量，旧码会被当成新码交回来，
   `/register` 只回一句「验证码不正确」（复现 6 轮里坏 4 轮，修好后 0 轮）。个别只吃整数的面板
   用 `{since_int}`。

### 3.3 错码重试策略（实测约束）

| 事实 | 数值 |
|---|---|
| 单个验证码可用尝试 | 5 次（第 6 次报 `验证码错误次数过多，请重新获取`） |
| 同号再次发码 | 60s 冷却（`验证码发送过于频繁，请 N 秒后再试`） |
| 重发后计数 | 复位（新码重新有 5 次） |

所以工具默认不重试；需要自动恢复时加 `--sms-resends`：

```powershell
# 错一次就等冷却、重新发码、再取一次（最多 2 轮）
python vmax_register.py --phone 13800138000 --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&timeout=180" `
  --sms-resends 2
```

判据（`vmax_register.classify_register_error`）：`code_wrong` / `code_locked` / `code_expired` 会触发重发，
`phone_format` / `cooldown` / `captcha_rejected` 直接失败，不做无意义重试。

### 3.4 写接口限流（比短信更早卡住）[实测 2026-10-26]

`POST /register`（表单）与 `POST /api/auth/register`（JSON）**共用一个按出口 IP 计数的桶**：

| 观测 | 数值 |
|---|---|
| 突发 | 空桶 ~3 秒内 7 次写可以，**第 8 次** 429（R75） |
| 持续 | 同一出口 IP 11 次写跨 ~18 分钟就撞（≈37 写/h 足够触发，R154） |
| 响应 | `操作过于频繁，请稍后重试`；无 `Retry-After` |
| 封禁 | 首封 ≥200 s；+380 s / +426 s 探测仍 429；**封禁期内轮询会续封**（429 也计数），一次封禁**活过 27 分钟静默** |
| 桶键 | **出口 IP**：16:20 换**新 device + 新号码**、同一个 IP，距上次失败 27 min 第一次写仍 429 ⇒ 不是设备/号码（`evidence/_r154d_scope.json`） |
| 不受影响 | GET 类、`POST /api/auth/login`、两个 `send-code` |

工具链行为：

```powershell
python vmax_pacer.py status          # 当前桶状态：banned / ban_remaining_s / strikes
python vmax_pacer.py reset           # 人工清空（换 IP 后可清）
```

- 默认 **5 次/600 s（=30 写/h）**，429 后 600s→1200s→…→7200s 指数退避
- 「5 次/60 s = 255 写/h」只是**突发上限**（空桶连发 7 次可以）：可持续预算 ≈ **30 写/h/IP**，
  实测 ~37 写/h 就撞；按 255/h 排产会先换来十几分钟的封禁
- 状态落 `.vmax_pace.json`，`vmax_batch --workers N` 的多个进程共享
- 封禁期**只 sleep 不探测**；`VMAX_NOPACE=1` 仅用于单次人工排查
- 想提速只有一条路：**每 worker 一个独立出口代理**，桶键会变成 `register@<proxy>`
  - 注意桶键是**代理 URL 字符串**，不是它真实落地的出口 IP：两个不同 URL 指向同一个出口，
    节流器会以为是两条车道而双倍放行 —— 加 IP 前先用 `vmax_doctor.py --proxy …` 看一眼真实出口
  - 真实产能按 **30 写/h × 不同出口数** 算，用 `vmax_plan.py --ips N` 反推
- **封禁期不发短信**：`send-code` 不在限流桶里，所以封禁中发码会「成功」，码到了 `/register` 却一直 429。
  现在 `register_one` 在发码前先 `Pacer.wait_if_banned()` 睡到解封（记在结果的 `waited_out_ban_s`），
  一个实卡短信和一个滑块 token 都不会白烧。
- 429 的那一行落盘是 `register_error_kind=rate_limited` + `register_http_status=429`（错误串里也带 `429`）：
  `vmax_go` / `vmax_batch` / `vmax_day` 三层据此**立刻拉闸**（exit 3），不会把限流当普通失败继续往下烧 token。

### 3.5 免短信复验 / 续期已有账号（`vmax_login.py`）[R97 / R102]

注册拿到的 JWT 只有 **3600 s**。账号建好以后，`POST /api/auth/login`（账号密码登录）能直接
重新签发 JWT —— **不碰短信、不碰号池**，代价是**每次真正走到滑块校验的尝试 1 个 token**
（密码错是 401，发生在滑块之前，**不消费** token，见下方口径）。

```powershell
python vmax_login.py --input accounts.csv --out logins.jsonl --captcha queue --queue-dir tokens
python vmax_login.py --input results.jsonl --out logins.jsonl --limit 5 --delay 2 --save-token
```

输入就是同一份 `accounts.csv`（`phone,username,password`）；`vmax_register` / `vmax_batch`
写出的 `results.jsonl` 也可以直接喂进去（带 `username` / `password`），但**只验 `status=ok` 的行**：
没建成的号没有账号可登，拿我们生成的密码去试别人已注册的号码既白烧一个 token，也是往风控里塞一次错密码。
纯 `cards.csv` 没有 `status` 列，仍然全验（手工核查用）。
**只有 `phone,password` 也行**：openapi 的 `LoginRequest` 写明「使用用户名或中国大陆手机号验证密码」，
缺 username 时客户端自动用手机号顶上，并在结果里标 `username_used_phone: true`
（summary 里 `phone_as_username=N` 记的是「靠手机号当用户名的**尝试**数」，不是成功数）。

| 状态 | 含义 | 续跑时 |
|---|---|---|
| `ok` | 拿到新 JWT（`uid` / `expires_in` / `token_masked`） | 跳过 |
| `auth_failed`（401） | 账号或密码错 | **终态**，跳过 |
| `no_credentials` | 这行没有 username/password | **终态**，跳过，0 token |
| `captcha_required`（403） | 滑块没过 / token 已用掉 | 重试（token 已经花了） |
| `rate_limited`（429） | 登录侧也限流 | 立即停（exit 3），**不要轮询** |
| `transport` / `http_<code>` | 网络或其它状态码 | 重试 |

口径（和注册不一样，容易踩）：

- **密码在滑块之前校验**（生产实测 2026-10-26：不存在的账号 + 假 token → `401 账号或密码错误`）：
  密码错的那一次**不消费** token，下一行可以接着用（`--token-reuse-seconds`，默认 60 s，0 = 不复用）。
  只有 200 / 403 / 429 和网络异常才算「token 没了」。仍然别拿它当密码猜测工具 —— 猜中就要花 token。
- 每天可用 `python vmax_rehearse.py --login-probe-only` 复查这条顺序：1 个请求、0 个滑块、0 条短信，
  输出 `LOGIN_READY=True order=auth_failed_first`（若哪天变成 `captcha_first`，说明服务端改了顺序，
  token 成本模型要跟着改）。
- 401 是终态（真错了），403 不是（换 token 还能过）。
- 默认**不落** `access_token`（那是一条活凭证），只存 `token_masked`；确实要落盘才加 `--save-token`。
- 输出 `logins.jsonl` + `logins.jsonl.summary.json`
  （`ok` / `fail` / `by_kind` / `captcha_tokens_spent`（真正消费）/ `tokens_popped` / `tokens_reused` / `aborted_on_429`）。
- 退出码：0 全 ok、1 有失败、2 输入为空、3 撞 429、4 没有可复验的号（输入里没有任何 `status=ok` 的行）。

### 3.6 回收号：把已有账号拿回来（`vmax_reset.py` / `--on-taken reset`）[R105]

§3.1.4 把 `phone_taken` 当终态，是为了不白烧 token；但**卡本身不必扔**。站点有标准找回流程，
而且第二步不需要滑块（openapi `ResetPasswordRequest` 只有 phone / sms_code / new_password）：

```powershell
# 单个号
python vmax_reset.py --phone 13800000000 `
  --sms "http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --captcha queue --queue-dir tokens

# 把一轮结果里所有 phone_taken 的号补回来（追加进同一个 results.jsonl）
python vmax_reset.py --input runs\1026\results.jsonl --captcha queue --queue-dir tokens

# 或者开跑时就决定：撞到老号立刻重置，不跳过
python vmax_go.py ... --on-taken reset        # vmax_batch / vmax_day 同名参数
```

| 事实 | 数值 |
|---|---|
| 每个号成本 | 1 个滑块 token + 1 条短信（与注册相同） |
| 最后一步 | **不需要滑块**，直接回 `access_token`（3600 s） |
| 成功后的行 | `status=ok` + `via=reset` + 新 `password` / `uid` → `vmax_login` 可直接复验 |
| `404` | 这个号其实没注册（`not_registered`）→ 回去走注册 |
| `429` | 该号当天发码次数用尽（`daily_limit`）→ **exit 3**，明天用同一个 `--out` 续跑 |
| 汇总 | `vmax_report` 把 `via=reset` 单独算 `reused=N`，不混进「新注册」 |

日限（`429 发送频率或每日次数超限`）在三个 runner 里的行为不同，这是故意的：

| runner | 撞到日限 | 为什么 |
|---|---|---|
| `vmax_reset.py` | 立即停 → **exit 3** | 这个进程只做重置，继续跑就是每个号白烧一个 token |
| `vmax_go.py` / `vmax_batch.py` | **两个不同号**都答 429 才关闭重置通道 → exit 7 + `reset_limited: true`，注册照常跑完 | 一次 429 可能只是那个号；两次说明不是单个号的问题。老号留给明天，新号不该陪葬 |
| 同一天再跑 | 输入/输出里当天 `daily_limit` 的行被跳过（**0 token**） | 429 在滑块闸之后，重试一次就是一次真实消耗 |

`--retry-limited` 才强制重试当天已限的号（站点重置了配额，或换了出口想赌一把）。
实测（`evidence/_r106_cap_stop.log`：4 个已注册号 + 1 个新号，全部被限）：
旧版 4 token，新版 **2 token**，两边都把新号注册成功；新版 summary 多一个 `reset_limited: true`。

边界：

- **纯技术收益**：`/register` 成功但结果行没落盘（崩溃/断电）时，账号存在而密码丢了 → 重置是唯一回收路径。
- **回收号**：卡片的上一个主人可能已经注册过。这是站点自己的找回流程，但会让原主人失去访问；
  只在「这张卡/这个号确实归你」的前提下用，并注意号源合规与站点条款。
- 默认仍是 `--on-taken skip`（exit 7 语义不变）；要用才显式开。
- 整条日线（`vmax_day --on-taken reset` → 复验 → 汇总 → manifest）由 `tests/test_day_e2e.py` 的 [5]、[6]
  两段真子进程覆盖：[5] 混合池回收成功 → exit 0、`reused=1`、两个号都能用新密码登录；
  [6] 回收通道被日限关掉 → exit 7、判定 `pool_had_taken_reset_capped`、新号照注册；
  [7] 跨天救援：同一份 results 复制成两天（第二天 `ts` 整体 -86400）—— 当天再跑 exit 7（`skipped_capped=2`、0 短信、0 新行），
  隔天 exit 0（`reused=2`、2 条 reset 行全 ok、2 条短信、复验 3 个号全 ok）。

### 3.7 一张卡回答 PROTOCOL 剩下的问题（`vmax_onecard.py`）[R122 / R133 / R141]

`docs/PROTOCOL.md` 里还剩四个只能靠一条真实短信关掉的问号：**码能不能跨设备 / 跨 session 用**、
**发码与提交是不是必须同一个 session**、**码到底活多久**、**发过码但请求里不带 `sms_code` 会怎样**
（openapi 说它非必填，业务上又像必填 —— 这是唯一可能变成「注册绕过」的那一格）。
买第一批卡之前先各花一条短信问清楚，比事后拿一整批卡去撞便宜得多 —— 四个问题加起来不到 5 条短信。

```powershell
# ① 跨 session：A 发码，B（独立 cookie jar、独立 PKCE/state、独立 captcha token）提交
python vmax_onecard.py xdevice --phone 13800138000 `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --out runs\onecard\xdevice.json --results runs\onecard\results.jsonl `
  --contract contract.json

# ② 码的存活时间：在 60 s 与 600 s 之间二分（--rounds 3 = 最多 3 条短信）
python vmax_onecard.py ttl --phone 13800138000 `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --low 60 --high 600 --rounds 3 --out runs\onecard\ttl.json `
  --contract contract.json

# ③ 码已发到，但表单里不带 sms_code（openapi 说非必填；1 SMS + 2~3 token）
python vmax_onecard.py nosms --phone 13800138000 `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --out runs\onecard\nosms.json --contract contract.json
```

| 模式 | 做什么 | 花费 | 结论（`verdict`） |
|---|---|---|---|
| `xdevice` | session A 发码 → 读码 → session B 提交 | 1 token + 1 条短信（R141 起表单默认走 device；signup-check 拒绝才 +1 token） | `cross_session_accepted` / `cross_session_rejected` / `inconclusive_expired` / `phone_taken` / `rate_limited` |
| `ttl` | 发码 → 等 `wait` 秒 → 提交，按成功/过期二分 | 每探 1 token + 1 条短信（R141 起；device 之前是 2 token） | `ttl_between`（`ttl_min_s` 是下界）/ `inconclusive_expired` |
| `nosms` | 发码 → 读码 → `/register` **不带** `sms_code`；被拒「码不对」再试 `sms_code=""` | 1 条短信 + 1 token（R141 起表单免费；被拒再试 `sms_code=""` 也不加 token，除非 device 被拒） | `accepted_without_code` / `accepted_with_empty_code` / `code_value_required` / `no_pending_code_seen` / `schema_rejected` |

怎么读：

- `cross_session_accepted`（B 拿到 303 + `code=`）→ 码跟**手机号**走：PROTOCOL §7「跨设备是否可用」与 §8.1 约束 3 可以按实测结论改写，农场与提交端彻底解耦。
- `cross_session_rejected`（`400 验证码不正确`）→ 码绑在发送的那个 session 里，保守做法继续有效：**发码与提交用同一个 jar**。
- `inconclusive_expired` → 等太久，不能证明也不能否定；调小等待时间重跑（`ttl` 模式自己会往下压）。
- `ttl` **成功即停**（号码已经建号，不能再往上探）：`ttl_min_s` = 至少活这么久，`ttl_max_s` = 上一次失败的点。
  两次探之间默认 `--cooldown 62`（同号重发 60 s 冷却，省不掉）。
- `accepted_without_code` / `accepted_with_empty_code` → **服务端只看「有没有待验证码记录」，不比对码值**：这是注册绕过。
  先按安全问题处理（verdict JSON 就是证据），不要拿它批量建号。
- `code_value_required`（`400 验证码不正确`）→ handler 确实比对 `sms_code`，缺失 / 空串都只是「码不对」；
  PROTOCOL §5.2.2 的问号关闭，工具继续按「必须带码」发。
- `no_pending_code_seen`（`400 请先获取短信验证码`）→ 刚发的码在 `/register` 那边看不到（记录作用域不同？），
  别下结论，换个号复跑一次。

产物与退出码：

- `--out` 的 verdict JSON：`tool` / `mode` / `verdict` / `elapsed_s` / 打码手机号，**不含密码、不含 token**，可直接当证据贴。
- `--results`（默认 `<out 去掉 .json>.results.jsonl`）：只有真建了号才写，行格式与 `vmax_day` / `vmax_go` 一致，
  可直接 `vmax_login` 复验、`vmax_report` / `vmax_fleet` 汇总。
- exit `0` 有结论；`1` 不确定（过期 / 超时 / 意外应答）；`2` 参数错；`3` 写桶 429（等 ≥200 s）；
  `9` 收码通道没起（**0 token / 0 短信**）。

> 它不回答的两件事：「一号一出口」的阈值（要多卡 + 多 IP，PROTOCOL §8 约束 2）、
> 「5 次错码预算耗尽后有没有号码级冻结」（要 6 次故意错码，属于另一条实验）。

### 3.8 实弹彩排（生产，不花短信、不建号）

正式跑号之前，先用真实站点把除「短信到达」以外的每一跳验证一遍：

```powershell
python vmax_rehearse.py        # 或 python vmax_go.py --rehearse
```

它做四件事：

1. `health` / `agreements` / `captcha-config` / `sms-config` 全绿（站点没改版、scene 没变）
2. 真解一个滑块 token（顺带测出当前解算耗时，正常 40–90 s）
3. 拿这个 token 打 `send-code` + `phone:"123"` → 期望 `手机号格式不正确`
   （滑块先于手机号校验，所以**短信不会发出去**，但 token 会被消费掉）
4. 再解一个 token 打 `POST /register` + 一个从未发过码的号码 → 期望 `请先获取短信验证码`

`READY=True` = 客户端每一跳都被生产接受了，只差真短信。失败会指出具体哪一跳：
`token_rejected`（求解器产出无效）、`token_missing`（参数没带上）、`rate_limited`（撞 §3.4 限流）。

> token 有效期只有 **90–120 s**（实测 76.5 s 有效 / 127.7 s 失效）。所以农场必须**边跑边供**：
> 队列深度 ≈ 并发数×2，`pop_token` 会自动丢弃 >90 s 的陈旧 token（`VMAX_TOKEN_MAX_AGE` 可调）。
> 一次解几百个放队列里 = 全废。

> 产能提醒：单浏览器上下文只能产 ~1 token/40–60 s（≈60–90 个/h），但**默认瓶颈不是滑块**，
> 而是一个出口 IP 的写预算（~30 写/h ⇒ ~25 账号/h，见 §3.4 / §8.1）。
> `vmax_tokens.py` 开 2 个上下文就够喂 2 个 IP；先加 IP，再加 solver。


### 3.8.1 崩溃窗口：`/register` 303 之后、行落盘之前 [R135]

这是唯一一处「账号已经真实存在，但本地什么都没留下」的时间窗：

```
POST /register  ->  303 + code      <- 服务端已核销短信、已建号
        ...（这一瞬间 Ctrl-C / kill / OOM）
写 results.jsonl                     <- 凭据只活在内存里，没写下来
```

旧行为：续跑时这个号看起来是新的 → `send-code` 回「该手机号已注册」→ 判成 `phone_taken` →
要么放弃（短信白花、账号拿不回来），要么 `--on-taken reset` 再花**一条**短信重置。

现在 `register_one()` 在 303 返回的那一刻就把凭据写进写前日志，`vmax_go` / `vmax_batch` 启动时先读它：

| 文件 | 何时写 | 内容 |
|---|---|---|
| `<out>.journal`（默认开） | 303 之后、token 交换之后各一行 | `phone` / `username` / `password` / `uid` / `ts`（**不存 access_token**） |
| 续跑恢复 | 启动时扫一遍，只补「日志里有 uid、`--out` 里连一行都没有」的号 | 直接追加一行 `status:"ok"`、`via:"journal"` |

恢复是**免费**的：凭据和 uid 都在日志里，不需要滑块、不需要短信。日志里能看到：

```
[go] journal: recovered 1 account(s) that were registered but never written -- 0 token, 0 SMS (runs\1026\results.jsonl.journal)
[go] 40 rows, 12 done, 28 to run
```

两条边界要记住：

- **只有 303 之后、token 交换之前**崩掉（约 200 ms）才会留下「没有 uid」的记录。这种号只能报出来，
  不会瞎猜：`journal: N number(s) reached /register 303 but the token exchange never finished`，
  想拿回来就走 `--on-taken reset`（1 条短信）。
- `--out` 里**已经有这个号任何一行**（哪怕是 `status:"error"`）就不再恢复 —— 是否重试由 runner 的
  状态机决定，日志不越权。想完全关掉：`--no-journal`（不读也不写）。

写盘用的是 `<out>.lock` 上的内核文件锁（Windows `msvcrt.locking` / POSIX `fcntl.flock`）+ 单次
`write()`。这不是过度设计：实测 8 线程 + 2 进程各 40 行（共 400 次追加）裸写只剩 **399** 个换行、
**397** 行能解析（两行被并进邻居、一行被吞）；放大到 1100 次追加实测丢 **60** 行（1063 换行 /
1040 可解析），每次新 handle 的写法丢 **423** 行。同一压力换成这个模块，3 轮全是 400/400，1100 次
也是 1100/1100。`results.jsonl` 和 `.journal` 里丢一行 = 一个付过钱的账号登不进去，所以两个文件
都走 `vmax_jsonl.append_lines()`。`.lock` 文件是运行期草稿，归档时可以删；不要手动编辑它。

全部靠恢复完成时退出码是 **0**，不是 4：这一轮虽然没有发任何短信，但确实把账号的行补上了。
`vmax_day` 跟着记 `manifest.journal_recovered`，并且它决定「要不要跑 0 短信复验」的门槛是
`ok > 0` **或** `journal_recovered > 0` —— 恢复出来的行恰恰是最该复验的一批（建号那次的 token 交换
可能根本没跑完），漏掉它们等于拿一批没验过的账号当绿。`vmax_fleet` 把它们按普通 `status=ok` 记账
（按 uid 去重、算当天 fresh），不需要额外参数。

归档时把 `<out>.journal` 和 `results.jsonl` 放一起：它是「这条行为什么算成功」的原始凭据，
也是下次续跑能免费认回账号的唯一依据。

## 3.9 上线前必跑：离线全链回归（不用真短信）

`tests/mock_e2e.py` 起一个本地假 auth 服务（同构于真实端点：`/login`、`/api/auth/agreements`、
`/register/send-code`、`/register` 表单、`/api/auth/oauth/token`），把**成功分支**跑通一遍：

```powershell
$env:VMAX_RESEND_WAIT = "0"     # 只为测试提速，生产别设
python tests/mock_e2e.py
```

覆盖 6 个场景：

| 场景 | 断言 |
|---|---|
| A 正常链 | 303 → `?code=` → PKCE 换 token → `uid` |
| B 错一次码 | 命中 `code_wrong` → 等冷却重发 → 成功 |
| C 60s 冷却 | 第二次发码 `ok:false`，归类为 `cooldown` |
| D PKCE 错配 | token 端点 `400 invalid_grant`，客户端抛 `VmaxError` |
| E 303 无 code | 解析成 `code=None` 且不崩，状态码保留 |
| F `vmax_verify.py` 全链 | 子进程跑真脚本，`FileSms` 取码 → `status:"ok"` |

> 它能证明**客户端逻辑**（表单字段、Origin、303 解析、PKCE、重试、JWT 解析）没问题；
> 不能替代真实服务端验证——服务端行为仍以 §7 的实测记录为准。

**35 套离线/实机测试，一条命令（约 174 秒）**

```powershell
python tests\run_all.py --log regress.log
# [run_all] 32 suite(s), pacer state per suite in %TEMP%\runall_xxx
# mock_e2e.py                      OK    pass=20  fail=0  exit=0   1.0s
# ...
# TOTAL suites=35 pass=1733 fail=0 bad=0 elapsed=170.6s
# ALL SUITES GREEN
```

它替掉以前手写的 PowerShell 循环，因为那个循环有两个坑：**不看退出码/不数 FAIL**（「全绿」只是人的说法），
而且**把工具目录里那份带真实封禁窗口的 `.vmax_pace.json` 泄漏给每个子进程** ——
一个无关的 mock 套件会因此 sleep 45 s，事后看起来像「偶发失败」。
现在每套都有独立的 pacer 状态 + `VMAX_RESEND_WAIT=0` + utf-8 stdio；
判绿要求 **exit 0 且没有 `FAIL ` 行**，否则整体 exit 1（`--only report,login` 只跑几套，`--list` 看清单）。

| 用例 | 覆盖 | 断言 |
|---|---|---|
| `tests/mock_e2e.py` | 客户端 6 个场景（成功/错码/冷却/PKCE/303 无 code/verify） | 20 |
| `tests/test_accept_history.py` | `--history` 多轮对比：排序/翻转/逐项 fail→ok | 45 |
| `tests/test_accept_live_mock.py` | `vmax_accept --live` 子进程 + `--yes` 拦截 + JSONL 解析 + **`smsd_modem` 三态**（没配模块=软过 / 报 `connected=false`=硬失败 / virtual=up）+ **R150 表单账：`live` 段带 `form_captcha` / `form_extra_tokens` / `device_ok`，`live_form_cost` 软行说清这笔钱** | 48 |
| `tests/test_batch_mock.py` | `vmax_go` 批量全链 + summary + 断点续跑（退 4/5/6）+ 429 拉闸 + **门禁看到活通道**、**R149 summary 的 `form_*` 字段（device 两卡：全 device / 1 SMS + 1 写/卡 / 0 额外 token）；R150 `form_reuse`：reuse 行不再落 none** | 43 |
| `tests/test_batch_parallel.py` | 4 号并发零串码 + 限流桶不丢更新 + `--on-taken reset` 的 4 并发熔断（旧 4 token → 2 token）与健康通道全回收 | 36 |
| `tests/test_day.py` | 当天编排：预检门禁、**`--contract` 契约门禁（关键漂移 exit 2 且不动批次）**、农场看护/重启上限、退出码语义、清单与密钥打码、`--retry-limited` 透传、**收码通道探活（http/file/cmd/none，401/无 modem/无 /health 回退）与 exit 9**、`channel_check` 统一入口（坏 spec 只出判定不抛异常）、**batch 半路 exit 9 时已完成的行照样复验+汇总**、**跑批看门狗阈值从 day 透传（`--sms-recheck` / `--sms-recheck-seconds`，0 = 关；`--no-sms-check` 同时关掉子进程的探活）** || 113 |
| `tests/test_docaudit.py` | **文档数字对账自身的回归**：UTF-16 日志、「一行点两个套件」归属最后一个、数字/total 漂移、未列套件、`--only` 部分日志、exit 0/1/2 | 16 |
| `tests/test_runall_log.py` | **`run_all --log` 自身的回归**（R132）：日志含每套件汇总行 + TOTAL + 判决、`vmax_docaudit` 直接可读、红套件写 `FAILED:`、`--quiet` 不改文件 | 24 |
| `tests/test_watchdog.py` | **跑批途中通道掉线**（R118）：go 死在第 2 行 → 2 行落盘 + exit 9 + summary 标记；`--sms-recheck 0 0` / `--no-sms-check` 关掉；batch 双车道与单车道都「不再动下一个号」 | 20 |
| `tests/test_entry_cost.py` | **入口成本闸门**（R147）：`--form-captcha` 在八个 runner 的 CLI 上存在且可选值正确、非法值 exit 2、默认值（七个 auto / onecard device）由 AST 读出、值真的透传到花钱那一层（go/panel → `register_one`，accept → 子命令，ready → day 命令）、`vmax_reset` 必须没有；删掉 panel 的 `add_argument` 做变异测试立刻 3 条红 | 35 |
| `tests/test_formcost.py` | **价目表只有一个家**（R152）：六桶价格、历史行「无价但免费」、未知标签不当 0、`summarize()` 字段集与不变式 `device+reuse+extra+unknown==行数`、**AST 扫描生产模块禁止第二份桶元组** | 39 |
| `tests/test_fleet_mock.py` | **fleet 账本**（R121，全离线）：`logins*` 跳过、四类分桶与坏行容忍、跨文件重复建号、uid 串号、四类分卡（fresh / was_taken / taken_today / already_ok）与本地午夜边界、`--write-fresh` 保留列、退出码 0/2/4、`--json` 无密码无 token | 39 |
| `tests/test_onecard_mock.py` | **一张卡一个协议结论**（R122，全 mock 0 支出）：`xdevice` 不绑 session → accepted、绑了 → rejected、码过期 → inconclusive（exit 1）、写桶 429 → exit 3、死通道 → exit 9 且 0 发送、`ttl` 向下二分与「成功即停」、verdict 文件 schema、建号行能直接喂 `vmax_login`、**`--contract` 契约门（漂移/不可达 → exit 4、0 短信）**、**`nosms` 四种 handler → 四种 verdict（R133）**、**R141 `--form-captcha device` 默认（信任/拒绝/仍拒三态 + `auto` 对照）**、**R142 `/login` 422 但发 cookie 时仍继续**、**R144 指纹格式（32 小写 hex）本地即拒** | 126 |
| `tests/test_contract_mock.py` | **契约漂移哨兵**（R123，全 mock 0 支出）：snapshot 不存正文只存 sha、一致→exit 0、10 类关键漂移各自 exit 1、新端点/版本升级默认不阻断（`--strict` 才 1）、readiness 变化只报 `[WARN]`、基线坏与站点不通→exit 2、离线 `diff` 同判 | 64 |
| `tests/test_form_captcha_retry.py` | 3 种表单 token 策略 + **R140 `device` 四态**（信任 → 零 token；signup-check 拒绝 → 买；/register 仍拒 → 买一个重试；通道报错 → 买且不丢卡）**+ R141 [8] 生产文案归类** | 39 |
| `tests/test_journal.py` | **崩溃窗口 / 写前日志**（R135，全 mock 0 支出）：303 后立刻落 WAL（凭据）、token 后补 uid；torn tail 与垃圾行容忍；`recover()` 幂等（已有任何一行就跳过）、有 uid 免费重建、没 uid 只报告；`vmax_go` 与 `vmax_batch --workers 2` 真跑：崩过的号**不再提交**、summary 记 `journal_recovered`；`--no-journal` 全关；**整池靠恢复完成 -> exit 0（不是 4）**、`vmax_day` 复验门槛含恢复数、`vmax_fleet` 当普通 ok 行 | 67 |
| `tests/test_register_503.py` | **503 退避重试**（R134，真 `register_one()` 驱动）：服务级 503 → 解新 token 重提同一码（1 短信 / 2 写）、`--retry-503 0` 直拒、两次 503 只多 1 token、运营商级 503 不重试、503→码错 → `code_wrong`、`retries_503` 落证据 | 28 |
| `tests/test_login_mock.py` | 免短信复验：注册→登录、401 终态、403 可重试、`--save-token`、手机号当用户名、401 后 token 复用 | 46 |
| `tests/test_pacer.py` | 限流桶/退避计算/跨进程状态 | 19 |
| `tests/test_capacity_model.py` | **写预算三处一致**（R157）：plan / pacer / go 必须同一个 30 写/h；一 IP 25.5 号/h、2 IP → 51 且 token 顶回绑定、0 IP → 永不；`go` 里不许再留 `RATE_PER_WINDOW`；文档必须写 30 写/h | 35 |
| `tests/test_panel_mock.py` | **面板批量**（R120，全 mock 0 支出）：`--count` 目标制与续跑只补差额、逐行落盘、异常变行不崩、`phone_taken`→exit 7、429→exit 3 只买 1 个、余额探针在第 2 单前失败→exit 9、空库存→exit 9 0 行、同号重复退单不重复注册、`--max-fail`、无 `--out` 只回内存、**R146 `--form-captcha device` 传到 `register_one` + summary 三计数 + mock 端到端（1 token/1 SMS/1 写，303）+ `auto` 对照行记为 `reuse` 而不是 device（R150）** | 83 |
| `tests/test_phone_taken.py` | 已注册号码：分类、probe 状态、exit 7、不计入 `--max-fail`、失败行保留 `send_code` | 38 |
| `tests/test_day_e2e.py` | 当天编排的真子进程：全链 / 429 / 全 taken / 混合池 / `--on-taken reset` 混合池 / 日限关通道 / **跨天救援（同一份 results 跑两天）** / **收码通道挂了 exit 9** / **[9] 直跑 `vmax_go` 与 [10] 直跑 `vmax_batch` 同样 exit 9 且 0 写 0 短信** / **[11] `--form-captcha device` 全链（1 短信 / 1 写 / 表单 0 token）**，验退出码、清单、复验范围与「taken 不花短信」 | 86 |
| `tests/test_plan.py` | 产能规划四约束 + deadline 反推 + 老号比例（token 墙/买号量）+ 回收口径（`--reset-taken`）+ 提交表单计价（`--form-captcha`/`--device-trust`，`reuse` 拒算）+ **R156 持续写墙**（`--write-per-hour` 30/h、突发 255/h 仍可复现、writes 默认绑定、deadline 报 `ips_needed`） | 145 |
| `tests/test_pop_token.py` | 取号互斥：8 线程 200 token 零重复零丢弃 | 22 |
| `tests/test_probe_mock.py` | 号源探针六态 + summary 分布 | 18 |
| `tests/test_ready_mock.py` | `vmax_ready`：一卡全链 → READY（六条链路 + 两条出处 + 花费 1/1/1）、死通道 exit 9 且 0 花、**已注册卡 → NOT_READY 并点名 `register_303`**、`--dry-run` 不建目录不打码漏密钥、**`--contract` 透传给 day**、**同一个 `--out` 再跑 → exit 2 `dirty_out` 且 0 花（R139：以前会假 READY）**、**`--resume` → `stale_artifacts`**、**日志恢复行 → `row_fresh`/`sms_this_run` 双 FAIL**、**`--form-captcha device` 透传 ready→day→batch→register（1 token + 1 SMS + 1 写，行 `form_captcha==device`、`device.signup_check.requires_captcha=false`）；ready.json 不含原始 row，证据只在 results.jsonl（R145）；**R151 默认 `auto` 的行记 `reuse`，`captcha_reused` 软行不再把 reuse 判成没复用（`cost.form_extra_tokens==0`）** | 64 |
| `tests/test_verify_mock.py` | **注册后的 0 短信复核**（R128）：`/session` 真认证、协议两版都 accepted、服务端丢弃 ids → 探针发现 → 补签一次并复查、补签缺 CSRF 头 → 403 只落证据、探针异常不改退出码 | 25 |
| `tests/test_rehearse.py` | 彩排判据分类 + `--login-probe` 顺序判定（密码先于滑块） | 24 |
| `tests/test_report.py` | `vmax_report`：taken 单列、rate=ok/attempted、BOM/脏行、float ts | 35 |
| `tests/test_reset_mock.py` | 回收号：重置成功/404/错码/滑块闸、`--on-taken reset` 端到端、控制组、日限熔断、当天 memo、预检探针、**隔天重救与 `--retry-limited`**、判定表里 9 归给收码通道 | 116 |
| `tests/test_journal_kill.py` | **真杀进程**（R137，全 mock 0 支出）：子进程对本地 mock 走真 HTTP，在 303 之后 / 换 token 之后分别 SIGKILL —— 盘上已有那条线、`results.jsonl` 一个字节都没有；没 uid 只报告、有 uid 重建完整行；无 access_token；**8 线程 + 2 进程同时 append 400 行不丢不并** | 34 |
| `tests/test_smsd_live.py` | 真起 daemon：鉴权/长轮询/`since` 亚秒/TTL/拒绑/`http:` provider | 39 |
| `tests/test_modem.py` | 实卡直连：PDU 编解码（GSM7/UCS2/UDH/字母发件人/SCTS）、CMGL 补收、CMGR、删除、去重、`+CMTI`/`+CMT`、text 模式、断线重连、PIN 锁卡、daemon 端到端 | 61 |
| `tests/test_tokens_farm.py` | 农场背压与退出顺序 | 41 |

### 4.0 一条命令跑完当天（`vmax_day.py`）[R103]

`vmax_go.py` 只管「预检 + 批量」。真到卡到手那天，还有三件事最容易漏：**农场没人起**
（队列空 → `no_token`，事后看起来像坏卡）、**跑完没人验证账号**（303 + JWT 也可能登录不通）、
**崩了没有一份「跑了什么、跑到哪」的记录**。`vmax_day` 把这几件事串起来：

```powershell
python vmax_day.py --input cards.csv `
  --sms "http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&since={since}" `
  --out runs\1026\results.jsonl --farm-workers 3 --contract contract.json --yes
```

顺序：`preflight` → **契约门**（传了 `--contract` 才跑）→ **收码通道探活** → 起农场（`vmax_tokens.py`，守护线程每 5 s 检查，死了重启，最多 3 次）→
`vmax_go` 批量 → `vmax_login` 免短信复验（只在 `ok>0` 时跑）→ `vmax_report` 两份汇总。

**每张卡的 token 成本** [R140]：`--form-captcha device` 让 `/register` 表单走站点自己的
「`GET /login` → `auth_fingerprint`(32 hex) → `GET /api/auth/signup-check`」链路；`signup-check`
答 `requires_captcha:false` 时表单带空 `captcha_verify_param` 也被接受（生产实测：回「请先获取短信验证码」，
说明人机门已过）。**发码那一步仍然必须给真 token**，所以一张卡 = **1 token + 1 短信**（原先要 1~2 个）。
任何一步不如预期（signup-check 拒绝 / 表单仍要 captcha / 通道报错）都会自动退回买 token 并照常注册 —
宁可多花一个 token，也不丢一张已经付过短信的卡。`--form-captcha` 四态：`auto`（默认，先复用发码 token，
被拒再解一个）、`reuse`（绝不补）、`fresh`（表单另解一个）、`device`（本段）。
`vmax_onecard` 三种模式默认也走 `device`（R141）：`xdevice` 1 token、`ttl` 每探 1 token、`nosms` 1 token。
入口覆盖面：`vmax_register` / `vmax_go` / `vmax_batch` / `vmax_day` / `vmax_ready` / `vmax_accept` / `vmax_panel` 都有这个开关，默认一律 `auto`（只有 `vmax_onecard` 默认 `device`）。R145 钉住 `ready → day → batch → register`，R146 钉住 `panel → register` 并让 `<out>.summary.json` 记 `form_captcha` / `form_device` / `form_fallback` —— 省 token 的入口必须能自证省了几个。

**多出口不受影响（R143 实测）**：设备信任**不绑出口 IP** —— 东京建信任、加州同 cookie 问 `signup-check` 仍回
`requires_captcha:false`，反向亦然，加州不带 cookie 的对照组回 `true`。所以每个分片/出口可以各自跑 `device`，
同一张卡的 `/login` 与 `POST /register` 不必钉在同一出口，换出口重试也不会退化成 `device_fallback`。
（这只证明「信任」跨 IP 有效，不代表「风控」跨 IP 无感。）

**信任键（R144 实测）**：`signup-check` 要求 `auth_device` 与 `auth_fingerprint` **同时存在**；
指纹只校格式（恰好 32 位小写 hex），**不校验值** —— 换一个新 hex 仍回 `requires_captcha:false`。
技术上「一份 jar + 任意合法 hex」就能过门，但站点自述指纹是「关联信号、服务端另有风控」，
所以仍按每卡随机 32 hex 走，不要为了省事改成常量。

**`auth_device` 是签名串（R148 实测）**：值形如 `<32hex id>.<unix 签发秒>.<64hex MAC>`，
`HttpOnly; Max-Age=2592000; Path=/; SameSite=lax; Secure`。同一 jar 再访 `/login` **不再下发** cookie
（30 天不滑动），改 id / 改 ts / 改 MAC 任一处 `signup-check` 立刻回 `requires_captcha:true`。
所以：**别手工编辑 jar**（想续命或换设备只能重新 `GET /login`），也**别为了省请求复用一份 jar 跑 N 个号**
—— 那会把 N 个账号串到同一个设备 id 上，省下的是 GET、赔掉的是关联风险。

探活只在**显式传了 `--sms`** 时跑（默认串口/接码地址不探，避免误报）：

```powershell
python vmax_sms.py --probe "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t"
# OK   health 200; modems=COM3:up; stored=0
# DOWN unreachable 127.0.0.1:8787: ConnectionError: ...
```

它检查三件事：端口能不能连、`secret` 对不对（401 直接判死）、`modems[]` 里至少有一个
`connected=true`（模块没插好/被 PIN 锁住会在这里暴露）。失败时 `vmax_day` 直接 `exit 9`，
**农场没起、token 没产、短信没发**；要强行跑用 `--no-sms-check` 或 `--force`。

直接跑单批（不走 `vmax_day`）也有同一道门禁 [R115]：`vmax_go` / `vmax_batch` 在**算完待跑号之后、
第一个 token 之前**探活，失败 `exit 9`（0 token / 0 短信 / 0 行结果），同样支持 `--no-sms-check` / `--force`。
与 day 的唯一区别：day 只在显式给了 `--sms` 时探（它的默认值只是占位地址）；
go/batch 默认就探——它们的默认通道（本地中继 / `sms_inbox` 目录）就是真会被使用的那一个。
待跑号为空（`exit 4`/`0`）时根本不探，免得把「今天没事可干」误报成通道故障。

**契约门** [R125]：`--contract 基线.json` 会在现场拉一份指纹并与基线比对（0 token / 0 短信），
结果三种：`0` 一致或只有警告 → 照常开跑；`1` 关键漂移 → **exit 2、`verdict=contract_drift`、批次没动**；
`2` 站点没答 → **exit 2、`verdict=contract_unavailable`**（网络问题，不是协议问题）。`--force` 才让它强行往下走。
日志在 `<run_dir>\logs\contract.log`，基线路径记在 `manifest.json` 的 `config.contract`。
**三个花钱入口都已接（R126）**：`vmax_day --contract`（漂移/不可达 → exit 2、`batch` 不动）、`vmax_onecard --contract`（在第一封 send-code 之前比，漂移/不可达 → **exit 4**、`verdict` 照写、0 短信）、`vmax_ready --contract`（原样透传给 day，门在 day 那边；`--dry-run` 会把绝对路径打出来）。没传 `--contract` 时三者行为与以前完全一致。

**跑到一半通道死了** [R118]：一个 300 号的批次要跑几小时，收码端在第 2 小时挂掉时，
剩下的每个号都会先烧 1 个 token，再等满 `--sms-timeout`（默认 300 s）才失败。
`vmax_go` / `vmax_batch` 现在**每 `--sms-recheck` 个号（默认 5）或每 `--sms-recheck-seconds` 秒（默认 300）**
复探一次；挂了就停在「下一个号之前」，已完成的行留在 `--out` 里直接续跑，退出码仍是 9：

```powershell
python vmax_go.py --input cards.csv --out results.jsonl --sms "http:..." `
  --sms-recheck 5 --sms-recheck-seconds 300      # 0 = 关掉这一类触发
```

从 `vmax_day` 起跑时不用改道：这两个阈值会原样带给子进程 [R119]，所以一个 300 号的日批
可以写 `python vmax_day.py ... --sms-recheck 10 --sms-recheck-seconds 600`（跑得久就放宽，
接码端不稳就收紧），`manifest.json` 的 `config.sms_recheck` / `config.sms_recheck_seconds`
留痕。`--no-sms-check` 也一并下发——它现在表示「这一趟任何一层都不探」，不再是
「day 不探、子进程照旧 exit 9」。子进程侧另有 `--sms-recheck 0 --sms-recheck-seconds 0`
只保留开跑前那一次闸门。

`vmax_day` 收到 batch 的 9 时**不会**跳过复验/汇总（只要 `results.jsonl` 已存在）：
已写下的号都是真账号，照样 `vmax_login` 复验 + `vmax_report` 出表，判定 `sms_channel_down`、exit 9。

| 产物 | 含义 |
|---|---|
| `runs\1026\results.jsonl` | 注册结果，格式与 `vmax_go` 完全一致，可直接续跑 |
| `runs\1026\logins.jsonl` | 每个 ok 号的密码复验（免短信、401 不烧 token） |
| `runs\1026\manifest.json` | 输入 SHA-256 与行数、每步退出码/耗时/日志路径、农场重启次数、判定 |
| `runs\1026\logs\*.log` | `preflight` / `batch` / `verify` / `report` / `farm` 的原始输出 |

判定 → 退出码：`ok` 0、`some_failed` 1、`preflight_failed` 2、`aborted_429` 3、
`nothing_to_do` 4、`stopped_max_fail` 5、`stopped_max_minutes` 6、`pool_had_taken` 7
（重置通道被日限关掉时是 `pool_had_taken_reset_capped` 7）、`farm_failed` 8、`sms_channel_down` 9（收码通道探活失败：开跑前挂是**一个 token 一条短信都没花**；半路挂时已完成的行照样复验+汇总，仍 exit 9。直跑 `vmax_go` / `vmax_batch` 同语义）、`verify_failed` 1（批量成功但复验失败，**不算绿**）、130 中断。
老号处理：默认 `--on-taken skip`（exit 7）；`--on-taken reset` 会用密码重置把老号救回来
（1 token + 1 SMS/号，§3.6），`manifest.json` 记 `on_taken`，汇总里 `reused=N`。

`--dry-run` 只打印将要执行的 6 条命令（`secret=` 已打码），不建目录、不发请求；
`--skip-farm` 用已有 token，`--no-verify` 跳过复验，`--force` 让失败预检放行。

## 4. 灰度：先 5 个号

```bash
python vmax_batch.py --input accounts.csv --out results.jsonl \
  --captcha queue --queue-dir tokens --workers 1 \
  --sms "http:https://api.example/code?phone={phone}" --delay 3 \
  --limit 5
python vmax_report.py results.jsonl
```

逐条看 `results.jsonl`：

- 每条必须只有一行，`status` ∈ {ok, error}
- `ok` 行必须有 `register.code` 与 `token.access_token`
- 失败行看 `error` 分类，不要盲目重跑（会再消耗 token）

### 4.1 跨天 / 跨车间的账：`vmax_fleet.py` [R121]

`vmax_report.py` 只看一个文件，`vmax_go` 的续跑只看同一个 `--out`。真实的一天会被拆成
分片、隔天重跑、面板补号 —— 同一个号可能被付两次钱（一次 token，或者一条短信 + 一次建号）。

```powershell
# 跑批前：这些卡里哪些已经在别处跑过？
python vmax_fleet.py --scan runs --check cards.csv --write-fresh runs\fresh.csv

# 跑完之后：整个 fleet 的账
python vmax_fleet.py --scan runs --json runs\fleet.json
```

| 问题 | 回答 |
|---|---|
| 这个号跑过吗 | `--check` 分四类：`fresh`（从没见过）、`was_taken`（以前被判已注册，**今天可以再试**，也计入 fresh）、`taken_today`（今天已被拒，**0 token 0 短信**该跳过）、`already_ok`（已有账号） |
| 有没有重复付钱 | `duplicate_ok`：同一个号在**多个文件**里都 `status=ok` |
| 数据有没有串 | `uid_collisions`：同一个 `uid` 出现在多个手机号下 |

`--write-fresh` 写回的列与 `vmax_day` / `vmax_go` 的输入一致（`phone,username,password,proxy`）。
`--file a.jsonl --file b.jsonl` 可以只算指定的几个文件（与 `--scan` 叠加）。

退出码：0 有 fresh 卡（或只跑汇总）、2 参数/路径错、4 全是 `already_ok` / `taken_today`（无事可做）。
`logins*.jsonl` 按文件名跳过（那是复验，不是注册）；没有 `phone` 的行记为 `skipped`，
解析不了的行记为 `bad`，都不致命。`--json` / 控制台都不含密码与 access_token。

## 5. 全量：并发 + 分片

单机：

```bash
python vmax_batch.py --input accounts.csv --out results.jsonl \
  --captcha queue --queue-dir tokens --queue-wait 600 \
  --workers 4 --sms "http:https://api.example/code?phone={phone}" --delay 2
```

多机（同一份输入切 3 份）：

```bash
# 机器 1 / 2 / 3 分别跑
python vmax_batch.py ... --shard 1/3
python vmax_batch.py ... --shard 2/3
python vmax_batch.py ... --shard 3/3
```

要点：

- `--workers` 只在 `--captcha queue` 生效（Playwright 非线程安全）
- `--queue-wait 600`：队列空时等农场补货，避免整批失败
- `--delay` 是**每 worker** 每号后的休眠；但真正上限是§3.4 的 IP 限流（~30 写/h，突发 5 次/60 s），**4 workers 共享一个直连 IP 只会更快 429**；多 worker 必须配每 worker 独立 `--proxy`
- 出口可以**逐行**给：`cards.csv` 的 `proxy` 列（表头 `phone,username,password,proxy`）优先于 `--proxy`；
  pacer 按 `register@<proxy URL>` 分桶，所以「一行一出口」才真的能并行（见 §3.4 的桶键提醒）
- 断点续跑：同一个 `--out` 重跑会自动跳过 `status=ok`

### 5.1 一条命令（推荐）

```powershell
# 先上盒子：预检（不会发短信、不会消耗滑块）
python vmax_go.py --preflight-only --queue-dir tokens

# 实跑：N 个实卡号 + 本机收码守护（vmax_smsd）
python vmax_go.py --input accounts.csv --out results.jsonl `
  --captcha queue --queue-dir tokens `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t" `
  --sms-resends 1 --yes
```

它会：

1. 预检 health/协议/滑块/**限流桶**；桶在封禁期直接退出（exit 2），不浪费短信
2. 打印 `lanes`（不同出口 IP 数）与 ETA 下界；单 IP 会明确提示「加 worker 只会更快 429」
3. 逐号跑，命中 429 **立刻停跑**（exit 3）并提示 `vmax_pacer.py status`
4. 结果写 `--out`，汇总写 `<out>.summary.json`；同 `--out` 重跑自动跳过已成功号

离线自检：`python vmax_go.py --dry-run`（跑 `tests/mock_e2e.py`，6 场景全 PASS）。

### 5.2 熔断、去重与退出码（实侧保护）

长跑最怕不是失败，是「一直失败一直烧信息」。两个参数把跑次关在笼子里：

```powershell
python vmax_go.py --input accounts.csv --out results.jsonl `
  --captcha queue --queue-dir tokens `
  --sms "http:http://127.0.0.1:8787/code?phone={phone}&secret=s3cr3t&timeout=180&since={since}" `
  --sms-resends 1 --max-fail 5 --max-minutes 120 --yes
```

| 参数 | 含义 | 触发后 |
|---|---|---|
| `--max-fail N` | 失败累计到 N 就停 | 写完 summary 后 **exit 5**，剩下的号不动 |
| `--max-minutes M` | 壁钟超时（每个号**开始前**先检查） | **exit 6**，不会因为一个慢号超过 M |
| 重复号码 | 自动折叠，只发一次短信 | 日志打印 `collapsed N duplicate phone row(s)` |

完整退出码：

| 码 | 含义 | 下一步 |
|---|---|---|
| 0 | 全部成功 | 存档证据 |
| 1 | 有失败（未触发熔断） | `vmax_report.py results.jsonl` 看分类 |
| 2 | 预检不通过（含 429 封禁中） | 修预检，不要 `--force` |
| 3 | 跑中撞 429 主动停 | 等 ≥200 s，**不要轮询** |
| 4 | 没事可做（全部已成功 / 已注册 / 当天额度用尽，且本次没有从写前日志恢复任何行） | 换 `--out` 或新号；恢复过行的情况是 **exit 0**（见 §3.8.1） |
| 5 | `--max-fail` 触发 | 先看 `error_kinds` 再恢复 |
| 6 | `--max-minutes` 触发 | 同一 `--out` 续跑即可 |
| 7 | 没有真失败，但有号被判 `phone_taken` 跳过，或当天回收额度已用尽（`skipped_capped`） | 正常：换新号或等明天；同一 `--out` 续跑不会再碰它们（`--retry-limited` 可强制当天再试） |

注：`--max-fail` / `--max-minutes` 会把 `vmax_batch` 强制为`--workers 1`（并行路径无法干净地“停在半截”）。

注：开了 `--on-taken reset` 之后，`taken` 只统计「重置也没救回来」的号；成功回收的算 `ok` 并单独计
`reused` —— 所以一池全是老号也可能 exit 0，那不是失败，是换了条路把号变成账号（§3.6）。

两个 runner 的 429 行为一致：`vmax_go` 串行遇 429 立即停（exit 3）；`vmax_batch --workers N` 用**拉取式**调度，
某条 lane 撞 429 后不再给任何 lane 派新号，等在跑的号收尾，同样 **exit 3**，并在日志里写「N row(s) not attempted」。
所以封禁时不会把余下几十个号全磨一遍、也不会把封禁越续越长。

日限（回收通道的 `429`）是另一套，它**不封注册写**：`vmax_reset.py` 立刻 exit 3；`vmax_go` / `vmax_batch`
要**两个不同号**都答 429 才关闭重置通道（exit 7 + `reset_limited: true`），注册照常跑完；
同一天再跑会跳过当天 `daily_limit` 的行（0 token），`--retry-limited` 才强制重试。
4 个全限号的实测：旧 4 token → 新 2 token（`evidence/_r106_cap_stop.log`），详见 §3.6。
并发下还有一层：reset 走**独立闸门**（一次只跑一个，进闸后重读开关）。不加闸门时 N 条 lane 会在「记下第一击」之前一起进重置 —— 实测 4 并发旧版全烧 **4 token**，现在固定 2
（`evidence/_r109_conc_breaker.log`）。

「明天再来」是真的能再来：`phone_taken` 只把号封到**当天**。续跑时 `--on-taken reset` 对「昨天判死」的号重新走一次注册→重置；对「今天已经 429 过」的号直接跳过（0 token，`skipped_capped` 计数，汇总 exit 7 ⇒ day 判 `pool_had_taken_reset_capped`）；`--retry-limited` 才强制当天再试。旧版把 taken 当永久黑名单，隔天也一个都不试
（`evidence/_r110_rescue.log`：旧 exit 4「nothing to do」→ 新 1 号救回 + 2 号 0 token 等明天）。
`vmax_day --on-taken reset --retry-limited` 会把同一个旗子透传给 batch 步骤（清单 `config.retry_limited` 留痕）。

### 5.3 并发正确性（`--workers > 1` 的实测结论，2026-10-26）

四个号同时跑（mock 服务 + 真 `vmax_smsd`，短信用不同延迟乱序到达）实测：

| 关注点 | 结论 | 证据 |
|---|---|---|
| 号与码是否串 | 不串。每号独立 `VmaxAuth` 会话 + 独立 PKCE/state；4 号并发后 mock 记录 `register_tries=4`、**错码尝试 0 次** | `tests/test_batch_parallel.py` |
| token 队列 | 已修：claim 改成 **`mkdir` 互斥锁 + 先读内容再改名**（rename 不能当 claim 原语，见 §2.1）。10 线程抢 6 个 token → 6 个不同 param、0 重复 | `tests/test_tokens_farm.py [7]` / `test_pop_token.py` |
| 限流桶 | 文件锁跨线程/进程生效：4 并发后 `window_used==4`，无丢失更新、无 429 | 同上 `[5]` |
| 退出码 | 已修 `--workers > 1` 末尾 `UnboundLocalError`（`stopped` 只在串行分支初始化），现在正常 exit 0 | 同上 `[1]` |
| reset 熔断 | 已修：4 条 lane 同时 `phone_taken` 时 reset 走闸门串行、进闸后重读开关；旧版 4 token，现在 2 token（cap 池）/ 4 个全回收（健康池） | 同上 `[7]` / `[7b]` |

两条踩过的坑（Windows 特有，别再改回去）：

- 把 token `os.replace(src, src + ".used")` 看似原子，实际是「先删目标再改名」：并发下**两个 worker 拿到同一个 token**，第三个在 `open()` 上报 `PermissionError`。唯一目标名才安全。
- `sorted(glob(...), key=os.path.getmtime)` 在文件被别的线程抢走时会直接抛 `FileNotFoundError`。要先 `stat` 再排序，消失的文件跳过。

**什么时候才该加 worker**：`--workers` 不会突破 §3.4 的 IP 限流（~30 写/h，5 次/600 s 共享一个桶）。
只有每 worker 配独立 `--proxy`（独立出口 IP）才有线性收益；单 IP 加 worker 只会更快 429。

## 6. 运行中监控

```bash
python vmax_report.py results.jsonl
# rows/unique/ok/error/rate/throughput + top errors
```

| 现象 | 处置 |
|---|---|
| `send-code: rate limited (429)` | 降并发、换出口、指数退避 |
| `captcha queue empty` | 加农场 worker / 加大 `--queue-wait` |
| `Captcha verification rejected` | token 过期/已用；检查是否跨队列重复消费 |
| `验证码不正确` / `验证码已过期` | 接码延迟或取到旧码；核对手机号与时间窗 |
| `该手机号未通过运营商验证` | 换号段；客户端已预拦 170/171 |
| `请先获取短信验证码` | 该号发码没成功（多半是 captcha 或 429） |

## 7. 收口：第一次真实成功

拿到实卡后，用 verify 脚本留下完整证据：

```bash
python vmax_verify.py --phone 13800000000 --captcha assist --sms prompt \
  --out evidence/first_success.jsonl
```

要确认的四件事：

1. `register.status == 303`
2. `register.location` 形如 `https://dreammaker.vobilemax.cn/auth/callback?code=...&state=...`
3. `state_echoed == true`
4. `token.access_token` 存在，`claims.exp` 在未来

把这份 jsonl 存进 `outputs/evidence/`，即完成「协议 → 实测」的闭环。

## 8. 容量与成本（粗算）

| 项 | 数值 | 说明 |
|---|---|---|
| 农场 | 1 token / 20~60 s / worker | 4 workers ≈ 4~12 token/min |
| 农场（实测 2026-10-26，R103） | **2 workers：19 次尝试 / 3 token / 144 s = 1.25 token/min**（该时段单次成功率 15.8%，属「坏时段」） | 吞吐 ≈ 尝试速度 × 成功率；加 worker 近似线性，但成功率随时段在 16%~58% 波动，**先测再定价** |
| 消费 | 纯 HTTP，4~8 并发 | 主要延迟在等短信 |
| 滑块 | ~35%/次，6 次 ≈92% | 难时段会连挂，重要批次用 assist |
| 短信 | 0.3~3 元/条 | 按号源实际计费 |
| 失败重跑 | 重新消耗 token + 可能重发短信 | 先按错误分类处置 |

### 8.1 用实测参数算产能（`vmax_plan.py`，离线）

上面那张表是粗算；`vmax_plan.py` 把实测常数固化成公式，直接回答「要多久 / 卡在哪 / 还差什么」。

```powershell
python vmax_plan.py --target 100                       # 默认：1 个解滑块 worker、1 张卡、2 并发、1 个出口 IP
python vmax_plan.py --target 100 --ips 2               # 两条出口：写墙翻到 51/h，瓶颈立刻回到 token
python vmax_plan.py --target 500 --deadline-hours 6     # 反推：6 小时做完要几个 solver / 几张卡 / 几个 worker
python vmax_plan.py --target 500 --deadline-hours 6 --ips 4   # 手里的 IP 数够不够（OVER 那行会消失）
python vmax_plan.py --target 100 --json                # 机器可读
```

默认常数全部来自实测（§3.4、§6.6）：滑块 35%、单次 20 s、token TTL 105 s、
写限流 **30 写/h/出口 IP**（突发 5 次/60 s 只是上限；`--write-per-hour` / `--write-cap` / `--write-window` 都能覆盖）、
短信 45 s、单号首次错码率 15%（最多 5 次）。

判读示例（1 solver / 1 卡 / 2 并发）：

| 约束 | 上限 | 说明 |
|---|---|---|
| tokens | 28.9 账号/h | 每账号 2.18 个 token（送码 1 + 提交 1.18），token 一次性 |
| **writes** | **25.5 账号/h** | **30 写/h/出口 IP ÷ 1.18 次/账号（R156 持续值；5 次/60 s 的 255/h 只是突发）** |
| sms | 80 账号/h | 1 卡 × 3600 ÷ 45 s |
| workers | 144 账号/h | 2 并发 × 3600 ÷ 50 s |

结论一眼看出：**默认是 1 个出口 IP 的写预算先到 —— ≈25 账号/h ≈ 3.9 h / 100 号**，
不是一个 solver（28.9/h），也不是短信。开第二条出口 IP 之后瓶颈才轮到 token 墙，
再往上才是「1 张卡只能喂 80/h」。`queue_cap` 是「TTL 内最多能同时囤几个 token」（默认 0.7 个）——
token 不能提前囤，农场必须跟消费节奏对齐，这也是 `vmax_tokens` 只建议现产现用的原因。

deadline 模式反推：

```
TO FINISH 500 IN 6.0h (83.33/h)
  solvers  3
  cards    2
  workers  2
  writes   98.0/h vs cap 30.0/h   <-- OVER THE 429 CEILING
  ips      4 egress IP(s) at 30.0/h each
TO FINISH 500 IN 1.0h (500.0/h)
  solvers  18
  cards    7
  writes   588.2/h vs cap 30.0/h   <-- OVER THE 429 CEILING
  ips      20 egress IP(s) at 30.0/h each
TO FINISH 150 IN 6.0h (25.0/h)
  solvers  1
  cards    1
  workers  1
  writes   29.4/h vs cap 30.0/h
  ips      1 egress IP(s) at 30.0/h each
```

判读：`ips` 那一行是 R156 新加的可执行项 —— 6 小时 500 号不是加 solver 能解决的，
至少要 **4 个出口 IP**（1 小时做 500 要 20 个）；一个 IP 的甜点区就是 **~150 号 / 6 h**。
撞 429 首封 600 s 起、封禁期内探测会续封，所以宁可多开 IP 也不要压窗口。

滑块通过率 **35% 是采样值，不是常数**（§6.4：早间 21/60 单次命中）。要拿它排产，先重测一次：

```powershell
python vmax_tokens.py --n 20 --max-queue 4 --attempts 6 --out tokens
# [farm] done in 214s: produced=7 failed=3 attempts=41 queue_left=4
#   -> 通过率 = 7/41 ≈ 17%
python vmax_plan.py --target 100 --pass-rate 0.17
```

2026-10-26 晚上的一次现场复验只有 **2 个 token / ~14 次尝试**（R157，`evidence/_r157_rehearse*.json`），
远低于 35% —— 也就是说「1 个 solver = 63 token/h」是**好日子**的数字，差的时候可能只有 1/4。
farm 每轮都会打 `attempts=`，拿它和 `produced=` 相除就是这个时段的上限；`--pass-rate` 传保守值。

号池不干净时（回收号 / 共享池），先把老号比例量出来（`vmax_probe.py` 的
`by_status.already_registered`），再喂给规划器：

```powershell
python vmax_plan.py --target 100 --taken-rate 0.3
# tokens   2.60/account -> 260 total
# sms      118 messages
# pool     30% of numbers already registered -> buy 143 numbers for 100
#          (yield 70%; taken ones cost 1 token, 0 SMS)
```

口径：老号在 `send-code` 就被拒（§3.1.4），**花 1 个 token、不花短信、不占写配额**，
所以它只把 token 墙按 `1/(1-r)` 推高，`sms` / `writes` 两条墙不动；
要凑够 N 个真账号得买 `N/(1-r)` 个号。`--taken-rate 1` 会被拒绝（永远凑不够）。

开了 `--on-taken reset` 之后换一套口径（`vmax_plan.py --reset-taken`）：老号不是丢掉，而是走找回流程
（§3.6）—— **2 token + 1 SMS**（第 1 步发码 1 token、第 2 步无滑块 0 token），且**第 2 步不是 `/register` 写**，
所以写墙只降不升；买号量从 `N/(1-r)` 变回 `N`，瓶颈回到 token 墙。回收本身也可能失败
（号被人改过密码/日限），用 `--reset-ok 0.6` 把它折进 yield。

实测对照（`--target 100 --taken-rate 0.5`）：

| 口径 | 买号 | token/账号 | 短信总量 | 吞吐 |
|---|---|---|---|---|
| 默认 skip | 200 | 3.18 | 118 | 19.8/h |
| `--reset-taken` | **100** | **2.09** | 109 | **30.2/h** |
| `--reset-taken --reset-ok 0.5` | 134 | 2.78 | 112 | 22.6/h |

（`evidence/_r112_plan.log`）跨天救援本身已由 `tests/test_day_e2e.py` [7] 用两天同一份 results 验证。

### 8.2 提交表单的验证码从哪来（`--form-captcha`）

`POST /register` 是否还要再买一个 token，取决于表单验证码的来源。价目表只有一份（`vmax_formcost.py`），
规划器按已观测的六条路径计价，`--form-captcha` 选「打算怎么付」：

| 策略 | 含义 | token/写 |
|---|---|---|
| `fresh`（默认） | 每次提交都新买一个 token | 1 |
| `auto` | 先用发码 token，被拒再买一个重试（R154：发码那次已把 token 用掉，所以重试几乎总会发生） | 1（+1 次白写） |
| `reuse` | 只用发码 token，不重试 | **拒绝计价**（交付不了账号） |
| `device` | 靠 `auth_device` 信任免滑块，失效落回付费 | `1 - device_trust` |

```powershell
python vmax_plan.py --target 100                      # 默认 fresh：2.18 token/账号、28.9/h
python vmax_plan.py --target 100 --form-captcha auto  # 与 fresh 同价（2.18/账号、28.9/h）+ 提醒多一次白写
python vmax_plan.py --target 100 --form-captcha reuse # exit 2：不替「交付不了账号」的策略算钱
python vmax_plan.py --target 100 --form-captcha device --device-trust 1.0
```

**默认故意停在 `fresh`**：白嫖路径是观察值，不是服务端的承诺。TTL 探针（`evidence/_r150_ttl.jsonl`）
跑满 16 轮到 **T+80 min（4804 s）** 仍 `requires_captcha=false`（对照组无 cookie = `true`）；但真卡链路一次都没跑通，所以预算按「每次都买」算——
省下来的是惊喜，不是计划。

R154 把 `auto` / `reuse` 从「0 个 token」修正回来：`/register/send-code` 验证成功就把
阿里云 cert 消耗掉，同一个 param 再验一次只会得到 `Captcha verification rejected`（`evidence/probe_sendcode.json`）。
所以 `auto` 的「重试」几乎总会发生（多一次 `/register` 写），`reuse` 则是「0 token、零交付」的陷阱。

`--device-trust` 只对 `device` 生效，范围 [0, 1]（默认 1.0），越界直接 exit 2。

⚠️ `--form-captcha device` 只拆掉 **token 墙**（1 token/账号 ⇒ 63/h），**拆不掉写墙**：
一个出口 IP 仍然只有 ~25.5 账号/h（R156）。要真上量，先加 IP，再加 solver。

## 9. 注意

- 每个号独立 `auth_device`（独立 session），不要复用
- 发码与提交注册必须同一 session（`auth_device` 是否强绑未验证，保守处理）
- token 一次性；不要跨小时囤
- 真实短信有费用与打扰，测试时不要用随机号码