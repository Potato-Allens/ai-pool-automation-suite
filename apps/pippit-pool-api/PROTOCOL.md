# Protocol Notes

## 请求签名

Pippit Web API 使用请求路径、时间戳和固定版本参数生成 `sign`，并通过 `device-time` 头传递毫秒时间戳。

```text
sign = md5("9e2c|" + last7(path) + "|7|5.8.0|" + timestamp + "||11ac")
```

实际实现以 `pippit.py` 为准。签名算法可能随客户端版本变化，升级前先做只读探测。

## 任务链路

```text
submit_run -> get_thread -> asset_id -> clean_url -> download -> probe -> gate
```

- `thread_id` 不能复用；复用会拿到上一轮 asset。
- 生成完成后先取 asset，再换取无水印下载地址。
- 下载 CDN 可能要求特定出口或代理，直连 403 时切换出口重试。
- 质量门在本地用 ffprobe 校验分辨率、帧数、时长和水印标记。

## 错误分类

| 类型 | 处理 |
|---|---|
| 余额不足 | 换号 |
| 队列/限流 | 退避重试 |
| 上游 5xx | 重试，超过次数换号 |
| 无 asset | 重跑 |
| 质量不达标 | 重跑或人工复核 |
| 账号失效 | 标记 dead，移出池 |

## 安全

本文档不包含任何真实账号、Cookie、Key、服务器地址或代理凭据。
