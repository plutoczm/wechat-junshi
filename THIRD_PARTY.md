# 第三方组件与致谢

本项目依赖或对接以下第三方作品。它们不是本项目的一部分，各自适用自己的许可证和服务条款。

## goutoujunshi

- 仓库：https://github.com/shengjidaguai-china/goutoujunshi
- 作者：powerycy
- 许可证：MIT License，Copyright (c) 2026 powerycy

当前移动工作台可选从用户本地安装的 `GOUTOUJUNSHI_SKILL_DIR/references/knowledge/` 只读加载与当前话题相关的知识文档；不会执行上游记忆脚本。旧入口仍会读取更多 skill 内容。

## wechatauto-replica（当前 Windows 微信桥接）

- 仓库：https://github.com/fanyuantaier/wechatauto-replica
- PyPI：`wechatauto-replica`
- 固定版本：`1.2.3`
- 许可证：Apache License 2.0

`requirements-wechat.txt` 使用 `wechatauto-replica[guia]==1.2.3`。本项目通过它：

- 读取用户自己 Windows 微信 4.x 的本地数据库；
- 全量读取指定私聊历史；
- 按 `sort_seq` 增量读取新消息；
- 用 UIA/OCR 驱动本机微信发送文字；
- 使用发送前水位和数据库回读对发送结果做确认。

上游更新记录列出了微信 **4.1.13.65** 的适配。本项目没有把该库源码复制进仓库，只按依赖安装。

读取本地加密数据库会涉及密钥提取与解密实现。只应处理你自己的设备、账号和你有权处理的数据。

## wxauto4（旧版入口）

- 文档：https://docs.wxauto.org/
- 旧依赖：`wxauto4>=41.1.7`

`bot.py` 旧入口仍依赖 wxauto4，仅保留迁移和对照。其免费版本兼容范围不足以覆盖本项目当前目标微信 4.1.13.65，因此**新移动入口不使用 wxauto4**。

## DailyHotApi

- 仓库：https://github.com/dshuais/DailyHotApi
- 许可证：MIT License，Copyright (c) 2023 底层用户

移动工作台的 `mobile/trends.py` 可以读取 DailyHotApi 兼容端点的公开热榜。默认配置使用其公开演示 API，生产环境建议自行部署。

程序只向该端点请求固定的公开榜单路径，不把用户私聊文本、人物名、微信号或 DeepSeek 密钥发送给它；私聊与榜单标题的相关性匹配在本机完成。

部分榜单由 DailyHotApi 自身通过公开网页/接口聚合，其可用性、抓取规则和数据权利边界由相应上游及站点决定。

## DeepSeek API

- 官方 API：https://api.deepseek.com
- 当前模型：`deepseek-flash`（DeepSeek V4.1 Flash）

API 密钥由用户自己在后端环境变量中配置。模型调用按 DeepSeek 当时的服务规则与价格计费。程序不把 API 密钥放进浏览器、本仓库或热榜服务。

## 微信 / WeChat / 腾讯

“微信”“WeChat”“腾讯”及相关名称、商标和图标归其权利人所有。

本项目与腾讯/微信没有隶属、合作、认证或授权关系。第三方自动化、UI 驱动及本地数据库读取可能受到客户端更新、平台风控和用户协议影响；请自行评估并只操作你自己的账号。
