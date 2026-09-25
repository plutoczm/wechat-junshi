# 微信恋爱军师 · wechat-junshi

一个**Android 手机控制 + Windows 常驻微信桥接 + 本地长期记忆**的私人恋爱回复工作台。

它把 [goutoujunshi](https://github.com/shengjidaguai-china/goutoujunshi) 的关系分析能力接到你自己的微信，并解决两个核心问题：

- 同一个人有多个微信号时，**历史和关系记忆合并为同一个人**；
- 实际收消息、草稿、发送目标和重试状态仍然**按微信号严格隔离**，避免串号错发。

## 当前主架构

```text
Android 手机
   │ HTTPS：查看建议 / 控制 OFF-C-B / 合并人物 / 同步历史
   ▼
Windows 常驻机
   ├─ FastAPI 手机控制台
   ├─ SQLite 长期聊天记忆
   ├─ DeepSeek V4.1 Flash（deepseek-flash）
   ├─ 免费开源 wechatauto-replica 1.2.3
   │    ├─ 微信 4.x 本地数据库：完整历史 + 增量消息
   │    └─ UIA/OCR 发送 + 数据库回读确认
   └─ 公开国内热榜缓存
   ▼
微信桌面版 4.1.13.65
```

完整安装、Android 访问、历史迁移、隐私与验收说明见 **[MOBILE.md](MOBILE.md)**。

## 模式

| 模式 | 行为 |
| --- | --- |
| **OFF** | 只保存历史，不生成、不发送 |
| **C** | 自动生成建议，手机查看/复制/编辑，绝不自动发送 |
| **B** | 自动生成并发送；每段都要求本地微信数据库回读确认 |

若 B 模式出现“UI 已操作但数据库无法确认”，系统进入 **send_unknown**，不会盲目重试。你需要先在微信里核对。

## 同人多号记忆

```text
她（人物）
├─ 她-大号 -> 独立 cursor / inbox / outbox
└─ 她-小号 -> 独立 cursor / inbox / outbox
       └──────────────┐
                      ▼
               同一份人物长期记忆
```

聊天原文永远保留来自哪个号。合并账号只改变人物归属，不会把两个聊天窗口的消息缓冲或发送队列揉在一起。

## 快速开始（Windows）

建议 Python 3.12，并使用你的微信 4.1.13.65：

```powershell
py -3.12 -m venv .venv-mobile
.\.venv-mobile\Scripts\Activate.ps1
python -m pip install -r requirements-wechat.txt

$env:JUNSHI_ADMIN_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:DEEPSEEK_API_KEY = "你的 DeepSeek 官方 API 密钥"
python -m mobile
```

只读验收微信桥接：

```powershell
python -m mobile.selfcheck
python -m mobile.selfcheck --contact "好友备注"
```

打开 `http://127.0.0.1:8787`。Android 远程访问必须配置 HTTPS，详见 [MOBILE.md](MOBILE.md)。

需要长期常驻时，在把 `JUNSHI_ADMIN_TOKEN`、`DEEPSEEK_API_KEY` 等变量持久化为当前 Windows 用户环境变量后，双击 `7-install-autostart.bat` 注册登录自启动；`8-remove-autostart.bat` 可撤销。B 模式依赖交互式 Windows 登录会话和微信 GUI，不能作为无桌面的系统服务运行。

## 手机历史

程序读取 Windows 本地微信数据库。因此，如果完整旧历史目前只在 Android，请先用微信客户端自带的聊天记录迁移/备份功能将所需会话迁移/恢复到这台 Windows 微信，再在手机控制台绑定相应好友并点“同步完整电脑历史”。

## 热梗

后台可拉取微博、抖音、B站、知乎、贴吧、百度、头条、快手等公开热榜。**私聊文本不发送给热榜服务**，只在本地与缓存的公开标题做相关性匹配；模型只在自然、低风险的场景偶尔引用。

## 数据边界

- 聊天、微信数据库、管理令牌、DeepSeek 密钥均不进入 GitHub；
- 只有你为某个微信号单独开启“允许必要文字发给 DeepSeek”后，该人物中同样已授权账号的有限检索片段才会进入模型请求；
- 图片/表情不发送给模型；
- 群聊不支持；
- 草稿不算“已经说过”，只有手工确认或微信数据库回读确认的文字才写成你的真实发言。

## 旧版

`bot.py`、`archive.py`、`junshi.py` 是早期 wxauto4 实验入口，保留用于迁移/对照。它的旧微信版本限制和 JSONL 归档方案不代表当前主架构。

## 第三方与许可证

详见 [THIRD_PARTY.md](THIRD_PARTY.md)。

本仓库采用 [MIT License](LICENSE)，Copyright (c) 2026 plutoczm。

> 本项目与腾讯、微信、DeepSeek 及上述开源项目均无隶属、合作或官方认证关系。微信第三方自动化存在风控和兼容性风险，请仅处理你自己的账号与有权处理的数据。
