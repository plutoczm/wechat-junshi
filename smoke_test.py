#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档版自动回复的端到端冒烟测试。

用假的微信客户端替换 wxauto4，验证完整链路：
轮询发现未读 → 归档 → 切换校验 → 读取新消息 → 按档案分组 → 生成回复 → 发送。

额外验证：归档落盘、连发合并、多个号共用一份记录。
不碰真实微信，不发送任何东西。
"""

from __future__ import annotations

import shutil
import sys
import time
import types
from pathlib import Path

PROJECT = Path(r"D:\Projects\wechat-junshi")
sys.path.insert(0, str(PROJECT))

SENT: list[tuple[str, str]] = []


class FakeMsg:
    def __init__(self, sender: str, content: str, attr: str = "friend", mtype: str = "text"):
        self.sender = sender
        self.content = content
        self.attr = attr
        self.type = mtype

    def __repr__(self) -> str:
        return f"FakeMsg({self.attr}/{self.type}, {self.content[:20]!r})"


class FakeSession:
    def __init__(self, name: str, isnew: bool = False, new_count: int = 0):
        self.name = name
        self.isnew = isnew
        self.new_count = new_count
        self.content = ""
        self.ismute = False


class FakeResponse(dict):
    def __bool__(self):
        return True


class FakeWeChat:
    """可编排的假客户端。"""

    def __init__(self, *args, **kwargs):
        self.chats: dict[str, list[FakeMsg]] = {}
        self.unread: dict[str, bool] = {}
        self.current: str | None = None
        self.switch_fail_for: set[str] = set()

    def seed(self, who: str, msgs: list[FakeMsg]) -> None:
        self.chats[who] = list(msgs)

    def push(self, who: str, msg: FakeMsg, unread: bool = True) -> None:
        self.chats.setdefault(who, []).append(msg)
        if unread:
            self.unread[who] = True

    # --- wxauto4 接口 ---
    def GetMyInfo(self):
        return {"display_name": "测试小号"}

    def IsOnline(self):
        return True

    def GetSession(self):
        return [
            FakeSession(
                who,
                isnew=self.unread.get(who, False),
                new_count=1 if self.unread.get(who) else 0,
            )
            for who in self.chats
        ]

    def ChatWith(self, who, exact=True, force=False, force_wait=0.5):
        self.current = who

    def ChatInfo(self):
        if self.current in self.switch_fail_for:
            return {"chat_type": "friend", "chat_name": "__错误的会话__"}
        return {"chat_type": "friend", "chat_name": self.current}

    def GetAllMessage(self):
        msgs = list(self.chats.get(self.current, []))
        self.unread[self.current] = False
        return msgs

    def SendMsg(self, msg, who=None, clear=True, at=None, exact=False):
        SENT.append((who, msg))
        self.chats.setdefault(who, []).append(FakeMsg("self", msg, attr="self"))
        return FakeResponse(message="ok")


FAKE = FakeWeChat()
TEST_ARCHIVE_DIR = PROJECT / "data" / "_smoke_archive"


def install_fake() -> None:
    mod = types.ModuleType("wxauto4")
    mod.WeChat = lambda *a, **k: FAKE
    sys.modules["wxauto4"] = mod


def main() -> int:
    install_fake()

    # 用独立的归档目录，避免污染正式记录
    if TEST_ARCHIVE_DIR.exists():
        shutil.rmtree(TEST_ARCHIVE_DIR)

    import archive as archmod
    import bot as botmod

    cfg = botmod.Config.load(PROJECT / "config.json")
    object.__setattr__(cfg, "delay_range", (0.05, 0.1))
    object.__setattr__(cfg, "min_interval", 0.1)
    object.__setattr__(cfg, "poll_interval", 0.2)
    object.__setattr__(cfg, "merge_window", 0.3)
    object.__setattr__(cfg, "archive_enabled", True)
    object.__setattr__(cfg, "archive_recall", 30)

    # 造第二个好友和大号/小号共用档案的场景
    object.__setattr__(cfg, "whitelist", ["Jeremy", "Jeremy小号", "路人"])
    object.__setattr__(
        cfg, "profiles", {"Jeremy": "Jeremy", "Jeremy小号": "Jeremy", "路人": "路人"}
    )

    print(f"好友: {cfg.whitelist}")
    print(f"档案: {cfg.profiles}")
    print()

    key = botmod.load_api_key()
    if not key:
        print("找不到 API 密钥")
        return 1

    botmod.setup_logging(None)
    archive = archmod.Archive(TEST_ARCHIVE_DIR, keep_days=0, capture_all=True)
    ai = botmod.JunshiAI(cfg, key, archive=archive)
    bot = botmod.JunshiBot(cfg, ai, archive=archive)
    bot.start()
    assert bot.wx is FAKE, "假客户端没有生效"

    passed, failed = 0, 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  ✓ {label} {detail}")
        else:
            failed += 1
            print(f"  ✗ {label} {detail}")

    def wait_sent(timeout: float = 90) -> int:
        deadline = time.time() + timeout
        while time.time() < deadline and not SENT:
            time.sleep(0.3)
        time.sleep(1.0)
        return len(SENT)

    # ---------- 场景 1：首次轮询只建立基线
    print("【场景 1】首次轮询：建立基线，不回复历史消息")
    FAKE.seed("Jeremy", [FakeMsg("Jeremy", "三天前的旧消息")])
    FAKE.unread["Jeremy"] = True
    SENT.clear()
    bot.poll_once()
    check("未回复历史", len(SENT) == 0, f"发送={len(SENT)}")
    check("已建立基线", "Jeremy" in bot.seen)
    print()

    # ---------- 场景 2：归档落盘
    print("【场景 2】读到的消息写进档案")
    n = archive.count("Jeremy")
    check("档案有记录", n >= 1, f"条数={n}")
    recs = archive.recall("Jeremy", 10)
    has_old = any("三天前的旧消息" in r.get("content", "") for r in recs)
    check("旧消息已归档", has_old)
    print()

    # ---------- 场景 3：新消息触发回复
    print("【场景 3】新消息 → 生成并发送回复")
    SENT.clear()
    FAKE.push("Jeremy", FakeMsg("Jeremy", "她三天没回我消息了，我该怎么回"))
    bot.poll_once()
    bot._flush_all_pending()
    sent_n = wait_sent()
    check("已发送回复", sent_n > 0, f"内容={SENT[0][1][:40] if SENT else '无'}")
    if SENT:
        check("发给正确的人", SENT[0][0] == "Jeremy", f"who={SENT[0][0]!r}")
    print()

    # ---------- 场景 4：连发合并
    print("【场景 4】连发多条 → 合并成一批一起回应")
    SENT.clear()
    FAKE.push("Jeremy", FakeMsg("Jeremy", "在吗"))
    FAKE.push("Jeremy", FakeMsg("Jeremy", "我很难受"))
    FAKE.push("Jeremy", FakeMsg("Jeremy", "他把我删了"))
    bot.poll_once()
    time.sleep(0.6)  # 超过合并窗口
    bot._flush_all_pending()
    wait_sent()
    check("已回复", len(SENT) > 0, f"共 {len(SENT)} 条")
    print()

    # ---------- 场景 5：大号小号共用一份记录
    print("【场景 5】小号的消息和大号共用同一份记录")
    before = archive.count("Jeremy")
    FAKE.seed("Jeremy小号", [FakeMsg("Jeremy小号", "用另一个号发的消息")])
    FAKE.unread["Jeremy小号"] = True
    SENT.clear()
    bot.poll_once()  # 建立小号基线
    after = archive.count("Jeremy")
    check("写进了同一个档案", after > before, f"{before} -> {after}")
    check("档案名是 Jeremy", archive.count("Jeremy小号") == 0)
    print()

    # ---------- 场景 6：独立档案互不串台
    print("【场景 6】不同好友的档案互相独立")
    FAKE.seed("路人", [FakeMsg("路人", "路人的消息")])
    FAKE.unread["路人"] = True
    bot.poll_once()
    check("路人写进自己的档案", archive.count("路人") >= 1, f"条数={archive.count('路人')}")
    jeremy_recs = [r.get("content", "") for r in archive.recall("Jeremy", 50)]
    check("路人消息没进 Jeremy 档案", not any("路人的消息" in c for c in jeremy_recs))
    print()

    # ---------- 场景 7：非白名单忽略
    print("【场景 7】非白名单好友 → 忽略")
    SENT.clear()
    FAKE.push("陌生人", FakeMsg("陌生人", "帮我看看"))
    bot.poll_once()
    bot._flush_all_pending()
    time.sleep(3)
    check("已忽略", len(SENT) == 0, f"发送={len(SENT)}")
    print()

    # ---------- 场景 8：自己发的消息忽略
    print("【场景 8】自己发的消息 → 忽略")
    SENT.clear()
    FAKE.push("Jeremy", FakeMsg("Jeremy", "我自己说的", attr="self"))
    bot.poll_once()
    bot._flush_all_pending()
    time.sleep(3)
    check("已忽略", len(SENT) == 0, f"发送={len(SENT)}")
    print()

    # ---------- 场景 9：非文本忽略
    print("【场景 9】非文本消息 → 忽略")
    SENT.clear()
    FAKE.push("Jeremy", FakeMsg("Jeremy", "图片", mtype="image"))
    bot.poll_once()
    bot._flush_all_pending()
    time.sleep(3)
    check("已忽略", len(SENT) == 0, f"发送={len(SENT)}")
    print()

    # ---------- 场景 10：切换校验失败不发送
    print("【场景 10】切换校验失败 → 绝不发送")
    SENT.clear()
    FAKE.switch_fail_for.add("Jeremy")
    FAKE.push("Jeremy", FakeMsg("Jeremy", "这条不该被回复"))
    bot.poll_once()
    bot._flush_all_pending()
    time.sleep(4)
    check("未发送", len(SENT) == 0, f"发送={len(SENT)}")
    FAKE.switch_fail_for.discard("Jeremy")
    print()

    # ---------- 场景 11：无信息量消息不回复
    print("【场景 11】无信息量消息（'嗯'）→ 不回复")
    SENT.clear()
    FAKE.push("Jeremy", FakeMsg("Jeremy", "嗯"))
    bot.poll_once()
    bot._flush_all_pending()
    time.sleep(4)
    check("未发送", len(SENT) == 0, f"发送={len(SENT)}")
    print()

    # ---------- 场景 12：档案能渲染成提示
    print("【场景 12】档案渲染成模型可读的对话记录")
    recs = archive.recall("Jeremy", 10)
    rendered = archmod.Archive.render_for_prompt(recs, {"Jeremy": "Jeremy"})
    check("渲染非空", len(rendered) > 0)
    check("含发言人标注", ":" in rendered)
    print(rendered[:300])
    print()

    print("=" * 60)
    print(f"结果：通过 {passed} 项，失败 {failed} 项")
    print("=" * 60)
    bot.stop()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
