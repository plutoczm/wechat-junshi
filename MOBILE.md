# Android 手机优先工作台

本项目的新主入口是 `python -m mobile`。推荐拓扑：

```text
Android 手机浏览器
        │ HTTPS
        ▼
Windows 10/11 常驻机
  ├─ mobile Web/API
  ├─ SQLite 长期记忆
  ├─ DeepSeek V4.1 Flash
  ├─ wechatauto-replica 1.2.3
  │    ├─ 本地微信 4.x 数据库读取
  │    └─ UIA/OCR 发送 + 数据库回读确认
  └─ 公开热榜缓存（私聊内容不发送给热榜服务）
        │
        ▼
微信桌面版 4.1.13.65
```

Android 只负责查看建议、切换人物/微信号、同步历史、设置 OFF/C/B 模式和处理发送异常；微信数据库读取和发送动作都发生在你自己的 Windows 电脑上。

> 代码层已经接通免费开源桥接、B/C 状态机和完整桌面历史导入。GitHub Actions 只能验证代码和 Windows 依赖能安装；真实微信登录、你账号的本地数据库密钥提取、真机 HTTPS 访问以及实际发送仍必须在你的电脑上执行一次验收。

## 能力

| 能力 | 实现 |
| --- | --- |
| 同一人的多个微信号 | 每个微信号独立绑定、独立消息游标、独立发送队列；共同指向一个人物记忆 |
| 不同人物隔离 | 检索 SQL 按 `person_id` 约束，不靠提示词隔离 |
| 完整聊天长期保存 | 微信桌面数据库全量同步到工作台 SQLite；原消息保留来源账号和时间 |
| 增量监听 | 每个微信号维护独立 `cursor_seq`，落盘成功后才推进 |
| C 模式 | 新消息自动生成草稿，只在手机控制台显示，不发送 |
| B 模式 | 新消息自动生成并发送；每段发送都要求桥接层数据库回读确认 |
| 发送结果未知 | 标记 `unknown`，**绝不自动重试**；手机提示先去微信核对 |
| 多段回复 | 按序逐段发送；已确认的段落单独落盘，避免整批盲目重发 |
| DeepSeek | 官方 `deepseek-flash`，显式关闭 thinking，只使用最终 `content` |
| 表情包 | 可归档为占位/元数据，但不上传图片、不自动发送表情 |
| 公开热梗/热榜 | 后台拉取微博、抖音、B站、知乎、贴吧、百度、头条、快手等公开榜单；私聊只在本地做词项匹配 |
| 手机远程控制 | 响应式 Web UI；远程必须 HTTPS |
| 手工兜底 | 保留 JSON/JSONL 导入和手动粘贴 C 模式 |

## 关键设计：人物记忆与微信会话分离

例如同一个人有两个微信号：

```text
人物：她
├─ 微信号 A（大号） -> cursor A / inbox A / outbox A
└─ 微信号 B（小号） -> cursor B / inbox B / outbox B
       │
       └──────────────┐
                      ▼
                  人物共享记忆
```

A 的历史可以帮助理解 B 的上下文，但 B 发来的消息只会回复 B。合并人物不会合并消息游标、缓冲或发送目标；拆分人物会让受影响的未发送草稿失效。

## 回复模式

### OFF

只保存已同步历史，不自动生成、不发送。

### C

后台监听到该微信号的新文字后，自动生成建议并显示在 Android 控制台。你可以复制、手工编辑后发送，再点“已手动发送”写入真实已发送记录。

### B

必须同时满足：

1. 该账号已绑定到一个真实私聊好友；
2. Windows 微信桥接在线；
3. 该微信号单独允许必要文字片段发送给 DeepSeek；
4. 手机控制台明确选择 B 并启用后台监听。

发送时会再次检查好友显示名与绑定目标是否仍一致，并使用桥接的 `verify=True` 数据库回读。若 UI 动作看似执行但数据库无法确认，则状态为“发送结果未知”，不会自动重试。

## Android 手机上已有完整历史怎么办

桥接读取的是**运行 Windows 微信客户端的本地数据库**，不是直接读取 Android App 沙箱。

如果旧聊天目前只完整存在于手机，请先使用微信客户端自带的聊天记录迁移/备份功能，把要使用的会话迁移/恢复到这台 Windows 微信。完成后：

1. Android 控制台新建人物；
2. 为她的大号、小号分别建立账号槽位；
3. 在“搜索当前 Windows 微信好友”中分别绑定真实好友；
4. 对每个号点“同步该微信号完整电脑历史”；
5. 将这些账号槽位移动到同一人物；
6. 再分别设置 OFF / C / B。

同步是幂等的：稳定来源 ID 已存在时跳过；相同文本但不同消息 ID 会分别保存。

## Windows 安装

建议 Python 3.12。

```powershell
git fetch origin
git switch main

py -3.12 -m venv .venv-mobile
.\.venv-mobile\Scripts\Activate.ps1
python -m pip install -r requirements-wechat.txt
```

也可以直接双击 `4-install-mobile-workbench.bat`。安装脚本会在 `.vendor/goutoujunshi` 准备一份上游 skill 的只读本地副本；如果你已有自己的副本，设置 `GOUTOUJUNSHI_SKILL_DIR` 会优先使用你的路径。

`requirements-wechat.txt` 固定使用：

- `wechatauto-replica[guia]==1.2.3`
- FastAPI / Uvicorn / HTTPX 等工作台依赖

该开源桥接上游标注支持微信 4.1.12+，并在其更新记录中列出了 4.1.13.65 的适配。项目不再要求把你的微信降级到 4.1.8.107。

### 环境变量

```powershell
# 至少 32 字符；仅用于手机控制台认证
$env:JUNSHI_ADMIN_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"

# DeepSeek 官方 API 密钥，仅放在 Windows 后端
$env:DEEPSEEK_API_KEY = "在本机设置，不要提交 GitHub"

# 可选：上游 goutoujunshi 本地目录
$env:GOUTOUJUNSHI_SKILL_DIR = "D:\path\to\goutoujunshi"

# 本机浏览器先这样运行
$env:JUNSHI_PUBLIC_ORIGIN = "http://127.0.0.1:8787"

# 可选：自建 DailyHotApi；设空字符串可关闭公开热榜
# $env:JUNSHI_TRENDS_BASE = "https://your-dailyhot.example/"
```

启动：

```powershell
python -m mobile
```

或双击 `5-start-mobile-workbench.bat`。只读微信桥接验收可双击 `6-check-wechat-bridge.bat`。

默认会启动微信监听、历史桥接和公开热榜后台刷新。只想管理记忆、不碰微信时：

```powershell
python -m mobile --no-automation
```

## 本地验收

先做只读自检：

```powershell
python -m mobile.selfcheck
python -m mobile.selfcheck --contact "好友备注"
```

这两条不会发送消息。它会检查：

- `wechatauto-replica` 是否安装；
- 微信本地数据库能否读取；
- 是否能识别当前登录账号；
- DeepSeek 密钥是否存在。

需要明确验证发送链路时，可以对一个你确认安全的测试好友执行一次：

```powershell
python -m mobile.selfcheck --send-test "测试好友的唯一显示名"
```

只有**精确且唯一**匹配的私聊好友才会发送，并要求数据库回读确认；不要拿重要联系人做第一次发送验收。

## Android 访问

Python 后端默认只监听 `127.0.0.1:8787`。同一电脑可直接打开：

```text
http://127.0.0.1:8787
```

Android 远程访问必须放在可信 HTTPS 入口后面，并把浏览器实际访问的 origin 写入：

```powershell
$env:JUNSHI_PUBLIC_ORIGIN = "https://你的私有域名"
```

不要直接把 8787 明文暴露到公网。推荐用你自己控制的 HTTPS 反向代理和额外访问控制；管理令牌不要放 URL、聊天记录或 GitHub。

## 长期记忆

工作台不是把所有历史每次全部塞给模型。数据分三层：

1. **原始消息**：本地 SQLite 永久保存，按账号保留来源；
2. **重要背景**：你可以手动钉住偏好、约定和边界；
3. **回复上下文**：同一人物最近消息 + 与本次来信词项相关的历史 +重要背景。

当前检索为确定性的中文双字/英文词项检索，不宣称是向量语义搜索。原始完整记录一直保留，因此未来可以重建更高级索引而不损失证据。

## 公开热梗与隐私

默认通过 DailyHotApi 兼容端点后台获取公开榜单，缓存 5 分钟。工作台不会把私聊文本提交给热榜服务：

```text
公开站点 -> 热榜缓存
                 │
私聊文本 -> 本地词项匹配 -> 最多几个相关公开标题 -> DeepSeek
```

模型提示明确要求：热词只是可选语气素材，不是关于好友的事实；严肃、冲突、安全场景不要硬玩梗；不知道含义时不要编出处。

可配置：

- `JUNSHI_TRENDS_BASE`：DailyHotApi 兼容地址；空字符串关闭；
- `JUNSHI_TREND_SOURCES`：逗号分隔来源。

为了稳定和隐私，建议生产环境自行部署开源 DailyHotApi，而不是长期依赖公共演示端点。

## 隐私与安全

- 真实聊天数据库默认在 `~/.wechat-junshi/mobile.sqlite3`，仓库外；
- API 密钥、管理令牌、微信数据库和聊天均被 gitignore；
- SQLite 本身不是加密数据库，建议 Windows 开启磁盘加密并做好加密备份；
- Android 页面令牌只存在内存，不写 localStorage；
- 页面只用 `textContent` 渲染聊天，不执行聊天 HTML；
- 模型没有文件、浏览器、微信或搜索工具，不能自行调用发送；
- B 发送只能通过持久化 outbox 和固定微信绑定执行；
- 群聊不支持，不会被绑定；
- 绑定显示名发生变化或不唯一时，B 模式拒绝猜目标。

第三方微信自动化可能触发平台风控，也可能与微信协议约束冲突。请仅操作你自己的账号和你有权处理的聊天数据，先用 C 模式和测试联系人验收，再逐个开启 B。

## 测试

```sh
python -m pip install -r requirements-mobile.txt pytest
python -m pytest -q tests/test_mobile.py
python -m compileall -q mobile
node --check mobile/static/app.js
```

CI 还会：

- 在 Chromium 390px / 1280px 下跑离线手机 UI smoke；
- 在 Windows + Python 3.12 安装固定版本 `wechatauto-replica[guia]` 并做无账号 import smoke；
- 不使用真实微信、真实聊天或真实 API 密钥。

## 旧入口

根目录 `bot.py` / `archive.py` / `junshi.py` 是早期 wxauto4 方案，仅保留用于迁移和对照。它依赖旧微信兼容范围，不应再作为 4.1.13.65 的生产入口。

当前主入口：**`python -m mobile`**。
