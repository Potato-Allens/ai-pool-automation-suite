# Contributing

1. 不要提交任何真实账号、Cookie、API Key、服务器地址、手机号或代理账密。
2. 新增配置一律使用环境变量，并在 `.env.example` 中写占位符。
3. 提交前运行 `python tools/secret_scan.py`（如存在）或至少执行 `git diff` 自查。
4. 日志、数据库、浏览器 profile、成片、截图统一放在 `data/`、`logs/`、`outputs/`，这些目录已被 `.gitignore` 排除。
5. 保持每个子项目可独立运行，根 README 只做导航和架构说明。
