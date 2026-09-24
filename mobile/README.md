# 手机优先工作台

主入口：`python -m mobile`。

当前实现包括：

- Android 响应式控制台；
- 同一人物多微信号共享长期记忆；
- Windows 微信 4.1.13.65 免费开源桥接；
- 每个微信号独立的历史同步、增量 cursor、inbox/outbox；
- OFF / C 建议 / B 自动发送三种模式；
- DeepSeek `deepseek-flash`；
- 发送数据库回读确认与 unknown 不重试；
- 公开国内热榜后台刷新，并在本地与私聊做相关性匹配。

完整安装、手机访问、历史迁移、隐私和真机验收说明见仓库根目录的 [MOBILE.md](../MOBILE.md)。

旧 `bot.py` 是 wxauto4 实验入口，不应再用于当前微信 4.1.13.65。
