# Deployment

## 通用原则

- 仅监听 `127.0.0.1`，由 nginx / Caddy 终结 TLS。
- API、worker、farm、SMS gate 分开跑，失败互不影响。
- SQLite 使用 WAL；高并发场景换 PostgreSQL。
- 每个出口 IP 单独限流；429 时停止重试并等待冷却。
- 日志、数据库、Cookie、profile、成片全部放在未跟踪目录。

## systemd 示例

```ini
[Unit]
Description=AI Pool API
After=network.target

[Service]
Type=simple
User=app
WorkingDirectory=/opt/ai-pool
EnvironmentFile=/opt/ai-pool/.env
ExecStart=/opt/ai-pool/.venv/bin/python app.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

## nginx 示例

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8788;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 600s;
    }
}
```

## 备份

- 只备份 SQLite 数据库与配置模板，不备份 Cookie、浏览器 profile、成片。
- 备份文件加密后存放在独立位置。
- 定期轮换管理令牌、API Key 和代理账密。
