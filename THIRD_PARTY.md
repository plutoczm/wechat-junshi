# 第三方组件与致谢

本项目依赖或对接以下第三方作品。它们都不是本项目的一部分，各有自己的许可证。

## goutoujunshi（恋爱军师 skill）

- 仓库：https://github.com/shengjidaguai-china/goutoujunshi
- 作者：powerycy
- 许可证：MIT License，Copyright (c) 2026 powerycy

本项目的全部"军师"判断能力来自这个 skill。本项目**不打包**它的内容，
而是在运行时从本地已安装的 skill 目录读取 `SKILL.md` 与 `references/`，
把相关内容装配进模型提示词。

如果你分发本项目，请同时遵守该 skill 的 MIT 许可证（保留版权与许可声明）。
skill 目录可用环境变量 `GOUTOUJUNSHI_SKILL_DIR` 指定。

## wxauto4

- 项目：https://docs.wxauto.org/
- 免费版通过 `pip install wxauto4` 获取

本项目用 wxauto4 经 Windows UI Automation 操作用户本人已登录的微信桌面客户端。
wxauto4 是独立第三方项目，与腾讯/微信无隶属或合作关系。

wxauto4 免费版 41.1.7 有两个已确认的缺陷，本项目用绕行方案处理：

| 缺陷 | 表现 | 本项目做法 |
| --- | --- | --- |
| `AddListenChat` 调用不存在的 `Chat.GetNewMessage()` | 监听线程抛 `AttributeError` | 改用轮询 `GetSession()` |
| `LoadMoreCache` 调用不存在的 `load_more_message` | 无法回读历史消息 | 改为运行时边读边归档 |

## DeepSeek API

- 服务：https://api.deepseek.com

默认用 `deepseek-chat` 生成回复。需自备 API 密钥，按官方价格计费。

## 微信 / WeChat / 腾讯

"微信"、"WeChat"、"腾讯"及相关名称、商标、图标归其各自权利人所有。
本项目与腾讯无任何隶属、合作、认证或授权关系，仅为说明兼容对象而作必要指称。
