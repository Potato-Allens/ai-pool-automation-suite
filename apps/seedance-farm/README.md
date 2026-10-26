# Seedance 2.5 Farm

> Seedance 2.5 免费额度账号农场与量产调度：注册 → 领分 → 验分 → 号池 → 多出口出片 → 收割 → 质检。

## 状态

- 单条 30s / 1080p 成本约 120~126 分。
- 340 分新号约可出 2 条 30s，剩余约 100 分。
- 4 条出口并行实测约 105 条/小时（随账号池、代理和上游限流变化）。
- 交付契约：`1080x1920` / `900 帧` / `30.000s` / `CLEAN`。

## 架构

```text
register -> claim -> credit check -> account pool -> line scheduler
   -> prod/agent -> download -> gate -> harvest -> manifest
```

## 脚本

| 文件 | 作用 |
|---|---|
| `_pp_http.py` | Pippit HTTP 客户端：签名、积分、任务、asset、下载、探测 |
| `_r329_farm_reg.py` | 单号注册 |
| `_r329_claim2.py` | 领取免费任务积分并读余额 |
| `_r329_farm_us.py` | 批量注册 + 领取 + 入库 |
| `_r329_prod.py` | 单条生产：提交、轮询、下载、质检 |
| `_r329_agent.py` | 浏览器/协议 agent |
| `_r329_line_multi.py` | 多账号/多线路调度 |
| `_r330_harvest.py` | 成片收割、ffprobe 校验、manifest |
| `_r600_slot_reaper.py` | 清理多余 agent slot |
| `_r702_keeper.py` | 看护 API / 产线 / watcher |

## 快速开始

```bash
pip install -r requirements.txt
export PP_HOME="$PWD"
export PP_PORTS='127.0.0.1:21030'
export PP_INVITE_CODE='<INVITE_CODE>'
python _r329_farm_us.py 1 DEMO
```

单条生产：

```bash
python _r329_prod.py account.json DEMO "Vertical 9:16 30-second product film" \
  --secs 30 --res 1080p --ratio 9:16
```

## 环境变量

| 变量 | 说明 |
|---|---|
| `PP_HOME` | 项目根目录 |
| `PP_PORTS` | 本地代理端口列表，逗号分隔 |
| `PP_INVITE_CODE` | 邀请码，占位符，不要提交真实值 |
| `PP_PY` | Python 解释器路径 |
| `PP_FFPROBE` | ffprobe 路径 |

## 质量门

- `width=1080`、`height=1920`
- `nb_frames=900`
- `duration=30.000 ± 0.35`
- 水印检测 `CLEAN`
- 不达标重跑，连续失败标记账号/线路异常

## 限流与稳定性

- 账号维度冷却，避免同一账号连续被打回。
- `thread_id` 禁止复用，否则会拿到上一轮 asset。
- 任务心跳每 150s 写入，避免 stale guard 误回收。
- 429 时停止重试并等待解封，轮询会续封。
- 失败换号；余额不足直接跳过该号。

## 测试

```bash
python -m compileall .
python _r330_harvest.py --help
python _r600_slot_reaper.py --help
```

## 注意

- 需要 Chrome、可用代理出口和已登录账号。
- 不要在无桌面会话的 SYSTEM 服务里跑浏览器。
- 所有账号、Cookie、代理和成片必须放在未跟踪目录。
- 仅用于自有账号、已授权账号或合规测试环境。
