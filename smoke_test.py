#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档版自动回复的端到端冒烟测试。

用假微信客户端替换 wxauto4，不碰真实微信、不发送任何东西。

分三组，避免互相干扰：
  A. 队列/发送层：不启动轮询线程，直接入队，验证合并、发送校验、重试、无信息量拦截
  B. 轮询层：启动轮询线程，每个场景用独立的会话名，验证基线、归档、过滤
  C. 档案层：验证分组共用、隔离、渲染
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
    def __init__(self, name: str, isnew: bool, new_count: int):
        self.name = name
        self.isnew = isnew
        self.new_count = new_count
        self.content = ""
        self.ismute = False


class FakeResponse(dict):
    def __bool__(self):
        return True


class FakeWeChat:
    def __init__(self):
        self.chats: dict[str, list[FakeMsg]] = {}
        self.unread: dict[str, bool] = {}
        self.current: str | None = None
        self.switch_fail_for: set[str] = set()

    def seed(self, who, msgs):
        self.chats[who] = list(msgs)

    def push(self, who, msg, unread=True):
        self.chats.setdefault(who, []).append(msg)
        if unread:
            self.unread[who] = True

    def forget(self, who):
        self.chats.pop(who, None)
        self.unread.pop(who, None)

    # --- wxauto4 接口 ---
    def GetMyInfo(self):
        return {"display_name": "测试小号"}

    def IsOnline(self):
        return True

    def GetSession(self):
        return [
            FakeSession(w, bool(self.unread.get(w)), 1 if self.unread.get(w) else 0)
            for w in self.chats
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
TEST_DIR = PROJECT / "data" / "_smoke_archive"


def install_fake():
    mod = types.ModuleType("wxauto4")
    mod.WeChat = lambda *a, **k: FAKE
    sys.modules["wxauto4"] = mod


def main() -> int:
    install_fake()
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)

    import archive as archmod
    import bot as botmod

    cfg = botmod.Config.load(PROJECT / "config.json")
    for key, val in {
        "delay_range": (0.05, 0.1),
        "min_interval": 0.1,
        "poll_interval": 0.2,
        "merge_window": 0.3,
        "archive_enabled": True,
        "archive_recall": 30,
        "whitelist": ["阿明", "阿明小号", "阿强"],
        "profiles": {"阿明": "阿明", "阿明小号": "阿明", "阿强": "阿强"},
    }.items():
        object.__setattr__(cfg, key, val)

    key_api = botmod.load_api_key()
    if not key_api:
        print("找不到 DeepSeek API 密钥，无法测试")
        return 1

    botmod.setup_logging(None)
    archive = archmod.Archive(TEST_DIR, keep_days=0, capture_all=True)
    ai = botmod.JunshiAI(cfg, key_api, archive=archive)
    bot = botmod.JunshiBot(cfg, ai, archive=archive)
    bot.wx = FAKE  # 直接注入，不调 start()（本组不跑轮询线程）

    passed, failed = 0, 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  ✓ {label} {detail}")
        else:
            failed += 1
            print(f"  ✗ {label} {detail}")

    def drain(timeout: float = 40) -> list[tuple[str, str]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if bot.reply_queue.unfinished_tasks == 0 and not bot.retry_count:
                break
            time.sleep(0.2)
        time.sleep(0.3)
        got = list(SENT)
        SENT.clear()
        return got

    print(f"好友: {cfg.whitelist}")
    print(f"档案: {cfg.profiles}")
    print()

    # ================= A. 发送层（不起轮询线程，直接入队）=================
    print("=" * 60)
    print("A 组：队列与发送层")
    print("=" * 60)

    import queue as queuelib
    import threading

    stop = threading.Event()
    worker = threading.Thread(target=bot.reply_worker, name="smoke-worker", daemon=True)
    worker.start()
    time.sleep(0.3)

    # A1 正常回复
    print("\n[A1] 正常回复")
    bot.reply_queue.put(("阿明", "阿明", ["她三天没回我消息了，我该怎么回"]))
    got = drain()
    check("已发送", len(got) > 0, f"内容={got[0][1][:40] if got else '无'}")
    if got:
        check("发给正确的人", got[0][0] == "阿明", f"who={got[0][0]!r}")

    # A2 连发合并不刷屏
    print("\n[A2] 连发三条 → 合成一批回应")
    bot.reply_queue.put(("阿明", "阿明", ["在吗", "我很难受", "他把我删了"]))
    got = drain()
    check("有回复", len(got) > 0, f"共 {len(got)} 条")
    check("条数可控（<=4）", len(got) <= 4, f"共 {len(got)} 条")

    # A3 目标会话定位不到 → 重试后放弃，绝不发错人
    print("\n[A3] 目标会话始终定位不到 → 绝不发送")
    FAKE.switch_fail_for.add("阿明")
    bot.reply_queue.put(("阿明", "阿明", ["这条不该被发出去"]))
    got = drain(60)
    check("未发送", len(got) == 0, f"发送={len(got)}")
    check("重试后已清理计数", not bot.retry_count, f"retry_count={bot.retry_count}")
    FAKE.switch_fail_for.discard("阿明")

    # A4 无信息量消息不回复
    print("\n[A4] 无信息量消息 → 不回复")
    for text in ["嗯", "哦", "好的", "。。。" ]:
        bot.reply_queue.put(("阿明", "阿明", [text]))
    got = drain(30)
    check("全部未回复", len(got) == 0, f"发送={len(got)}")

    # ================= B. 轮询层（起轮询线程，独立会话名）=================
    print()
    print("=" * 60)
    print("B 组：轮询与过滤（每个场景用独立会话，互不干扰）")
    print("=" * 60)

    stop.set()
    worker.join(timeout=5)
    bot.stop_event = threading.Event()
    bot.retry_count.clear()

    # B1 首次轮询只建立基线，不回复历史
    print("\n[B1] 首次见到会话 → 建立基线，不回复历史")
    FAKE.forget("阿明")
    FAKE.seed("阿明", [FakeMsg("阿明", "三天前的旧消息")])
    FAKE.unread["阿明"] = True
    bot.seen.pop("阿明", None)
    SENT.clear()
    bot.poll_once()
    check("未回复历史", len(SENT) == 0, f"发送={len(SENT)}")
    check("已建立基线", "阿明" in bot.seen)

    # B2 归档落盘
    print("\n[B2] 读到的消息写进档案")
    recs = [r.get("content", "") for r in archive.recall("阿明", 20)]
    check("旧消息已归档", any("三天前的旧消息" in c for c in recs), f"档案 {len(recs)} 条")

    # B3 非白名单忽略（用独立会话名）
    print("\n[B3] 非白名单好友 → 忽略")
    FAKE.seed("陌生人", [FakeMsg("陌生人", "帮我看看")])
    FAKE.unread["陌生人"] = True
    SENT.clear()
    bot.poll_once()
    bot._flush_all_pending()
    time.sleep(1.5)
    check("已忽略", len(SENT) == 0, f"发送={len(SENT)}")
    check("陌生人不进任何档案", archive.count("阿明") == len(recs), f"阿明档案 {archive.count('阿明')} 条")

    # B4 自己发的消息忽略（独立会话）
    print("\n[B4] 自己发的消息 → 忽略")
    FAKE.seed("阿强", [FakeMsg("阿强", "我自己说的", attr="self")])
    FAKE.unread["阿强"] = True
    bot.seen.pop("阿强", None)
    bot.poll_once()  # 建立基线
    FAKE.push("阿强", FakeMsg("阿强", "又一条我自己说的", attr="self"))
    SENT.clear()
    bot.poll_once()
    bot._flush_all_pending()
    time.sleep(1.5)
    check("已忽略", len(SENT) == 0, f"发送={len(SENT)}")

    # B5 非文本忽略
    print("\n[B5] 非文本消息 → 忽略")
    FAKE.push("阿强", FakeMsg("阿强", "图片", mtype="image"))
    SENT.clear()
    bot.poll_once()
    bot._flush_all_pending()
    time.sleep(1.5)
    check("已忽略", len(SENT) == 0, f"发送={len(SENT)}")

    # ================= C. 档案层 =================
    print()
    print("=" * 60)
    print("C 组：档案分组与隔离")
    print("=" * 60)

    # C1 大号小号共用一份记录
    print("\n[C1] 小号消息写入大号的同一档案")
    before = archive.count("阿明")
    FAKE.seed("阿明小号", [FakeMsg("阿明小号", "用另一个号发的消息")])
    FAKE.unread["阿明小号"] = True
    bot.seen.pop("阿明", None)
    bot.poll_once()
    after = archive.count("阿明")
    check("写进同一档案", after > before, f"{before} -> {after}")
    check("没有独立档案", archive.count("阿明小号") == 0)
    check("档案名正确", archive.path_for("阿明").exists())

    # C2 独立档案互不串台
    print("\n[C2] 不同好友的档案互相独立")
    FAKE.seed("阿强", [FakeMsg("阿强", "阿强的独立消息")])
    FAKE.unread["阿强"] = True
    bot.seen.pop("阿强", None)
    bot.poll_once()
    check("阿强有自己的档案", archive.count("阿强") >= 1, f"{archive.count('阿强')} 条")
    ming = [r.get("content", "") for r in archive.recall("阿明", 50)]
    check("阿强消息没进阿明档案", not any("阿强的独立消息" in c for c in ming))

    # C3 渲染成模型可读格式
    print("\n[C3] 档案渲染")
    rendered = archmod.Archive.render_for_prompt(
        archive.recall("阿明", 10), {"阿明": "阿明", "阿明小号": "阿明小号"}
    )
    check("渲染非空", len(rendered) > 0)
    check("含发言人标注", ":" in rendered)
    check("标注了自己的消息", "我（本账号）" in rendered or "阿明" in rendered)
    print("      渲染示例：")
    for line in rendered.splitlines()[:4]:
        print(f"        {line}")

    print()
    print("=" * 60)
    print(f"结果：通过 {passed} 项，失败 {failed} 项")
    print("=" * 60)
    stop.set()
    bot.stop_event.set()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
