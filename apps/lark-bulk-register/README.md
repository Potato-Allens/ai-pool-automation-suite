# Lark Bulk Register

> 飞书 / Lark 协议注册与企业创建工具（历史项目，协议参考）。
> Frida 抓包 · 纯协议注册链 · 企业创建 · ADB UI 兜底 · 批量取号收码

## 功能

- 协议链路：`credential -> auth/code/apply -> auth/code/verify -> tenant/create -> set_name -> tenant/information`
- 批量注册：号码文件 / 接码平台 / 手工验证码
- 企业创建与初始化
- ADB UI 兜底：按屏幕文字点击登录、验证码、跳过、完成
- JSONL 记账、失败重试、号码状态分类

## 目录

```text
lark-bulk-register/
├─ lark_passport.py   协议核心
├─ batch_register.py  批量执行器
├─ ui_driver.py       ADB UI 兜底
├─ d1jiema.py         接码客户端
└─ requirements.txt
```

## 快速开始

```bash
pip install -r requirements.txt

# 只探测号码状态，不发短信
python lark_passport.py --phone <TEST_PHONE> --stage credential

# 单号全流程
python lark_passport.py --phone <TEST_PHONE> --stage drive --team-name "Acme Ltd"

# 批量（手工验证码）
python batch_register.py --phones phones.txt --manual --team-name "Acme{i}" --out accounts.jsonl
```

## 注意

- 飞书对新号注册有风控；虚拟号段、已注册号、封禁号通常无法收码。
- 本项目不包含真实号码、验证码、账号、Cookie 或企业信息。
- 仅用于自有账号、已授权账号或合规测试环境。

## 免责声明

协议可能随客户端版本变化；使用前自行验证目标平台服务条款与当地法律。
