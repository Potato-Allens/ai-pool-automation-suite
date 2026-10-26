# Doubao Video Kit

> 豆包免费视频额度的脚本化 CLI：查额度 → 取直链 → 下载 → 去水印 → 提交生成。

## 功能

- `quota`：查询剩余免费次数
- `play`：获取视频直链
- `dl`：下载视频
- `gen`：提交生成（默认 dry-run，显式 `--send` 才消耗额度）
- Cookie 输入：JSON / 环境变量 / 浏览器 DPAPI 自动解密
- 输出默认打码，避免泄露账号凭据

## 快速开始

```bash
pip install -r requirements.txt

# 查额度
python doubao_video.py quota --cookies /path/to/cookies.json

# 取直链
python doubao_video.py play --cookies /path/to/cookies.json

# 下载
python doubao_video.py dl --cookies /path/to/cookies.json --out out.mp4

# 生成（先 dry-run）
python doubao_video.py gen --cookies /path/to/cookies.json --prompt "..." 
python doubao_video.py gen --cookies /path/to/cookies.json --prompt "..." --send
```

## Cookie 输入

1. JSON 文件：flat JSON / list JSON / Cookie 头 / Netscape cookies.txt
2. 环境变量：`DOUBAO_COOKIES_JSON` 或 `DOUBAO_COOKIE`
3. Windows DPAPI：`--from-browser` 或 `--profile <Chrome profile>`

Cookie 等同账号凭据，禁止提交到仓库或发群。

## 目录

```text
doubao-video-kit/
├─ doubao_video.py   CLI
├─ harvest.py        批量收割示例
├─ examples/         提示词模板
└─ requirements.txt
```

## 注意

- `gen` 默认 dry-run，只有 `--send` 才真正消耗额度。
- 额度、模型和接口可能随时变化。
- 仅用于自有账号、已授权账号或合规测试环境。
