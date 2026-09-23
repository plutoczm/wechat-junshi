#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话档案：把机器人读到的微信消息持久化，并按"档案"分组召回。

为什么需要它
------------
微信不开放聊天记录，wxauto4 的 `LoadMoreCache` 也是坏的
（`WeChatMainWnd` 缺 `load_more_message`），UIA 也没有可滚动元素，
所以**无法回读历史消息**。唯一的办法是机器人运行时"边读边存"：
每次轮询读到某个会话的消息，就把看到的每一条写进本地档案。
跑得越久，档案越完整。

档案（profile）是什么
--------------------
一个档案 = 一段共享的对话上下文。默认每个好友各自一个档案；
但同一个人的大号和小号可以指向同一个档案，这样它们的对话记录就合并了。

存储
----
    data/archive/<profile>.jsonl    每行一条记录，追加写入

记录类型：
    {"t": "meta", ...}   档案元信息（目前是谁在用）
    {"t": "msg",  ...}   一条消息
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ARCHIVE_DIR_NAME = "archive"

# 只保留这些字段，避免把 UI 对象引用写进档案
FIELD_CAP = 2000


def fingerprint(person: str, msg: Any) -> str:
    """给一条消息算指纹，用于判断是否已经归档过。

    person 参与指纹，因为同一个人可能用多个号（多个会话窗口）发消息。
    """
    attr = str(getattr(msg, "attr", "") or "")
    mtype = str(getattr(msg, "type", "") or "")
    sender = str(getattr(msg, "sender", "") or "")
    content = str(getattr(msg, "content", "") or "")[:FIELD_CAP]
    return f"{person}\x1f{attr}\x1f{mtype}\x1f{sender}\x1f{content}"


def msg_to_record(person: str, msg: Any, replied: bool = False) -> dict[str, Any]:
    """把一个 wxauto4 消息对象转成可存储的记录。"""
    attr = str(getattr(msg, "attr", "") or "")
    return {
        "t": "msg",
        "ts": time.time(),
        "person": person,
        "sender": attr if attr in ("self", "friend", "system", "other") else "other",
        "type": str(getattr(msg, "type", "") or ""),
        "content": str(getattr(msg, "content", "") or "")[:FIELD_CAP],
        "fp": fingerprint(person, msg),
        "replied": bool(replied),
    }


def read_record_to_dict(person: str, msg: Any, replied: bool = False) -> dict[str, Any]:
    """同 msg_to_record，但额外保留原始属性字典（备份用）。"""
    record = msg_to_record(person, msg, replied)
    raw = getattr(msg, "raw", None)
    if isinstance(raw, dict):
        record["raw"] = raw
    return record


@dataclass
class Profile:
    """一个会话档案。"""

    name: str
    members: list[str] = field(default_factory=list)
    note: str = ""


class Archive:
    """按档案读写消息记录。线程安全（机器人有多线程）。"""

    def __init__(self, base_dir: Path | str, keep_days: int = 0, capture_all: bool = True) -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)
        self.keep_days = max(0, int(keep_days))
        self.capture_all = bool(capture_all)
        self._lock = __import__("threading").RLock()
        self._cache: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------ 路径

    def path_for(self, profile: str) -> Path:
        safe = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", profile)[:80] or "default"
        return self.base / f"{safe}.jsonl"

    # ------------------------------------------------------------ 写入

    def append(self, profile: str, records: Iterable[dict[str, Any]]) -> int:
        """追加若干记录，返回实际写入条数（已存在的指纹会跳过）。"""
        path = self.path_for(profile)
        with self._lock:
            existing = self._fingerprints(profile)
            to_write = []
            for rec in records:
                fp = rec.get("fp")
                if fp and fp in existing:
                    continue
                if fp:
                    existing.add(fp)
                to_write.append(rec)
            if not to_write:
                return 0
            with path.open("a", encoding="utf-8") as fh:
                for rec in to_write:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # 缓存失效，下次召回重新读
            self._cache.pop(profile, None)
            return len(to_write)

    def _fingerprints(self, profile: str) -> set[str]:
        return {r["fp"] for r in self._load(profile) if r.get("fp")}

    # ------------------------------------------------------------ 读取

    def _load(self, profile: str) -> list[dict[str, Any]]:
        if profile in self._cache:
            return self._cache[profile]
        path = self.path_for(profile)
        records: list[dict[str, Any]] = []
        if path.exists():
            cutoff = time.time() - self.keep_days * 86400 if self.keep_days else None
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cutoff is not None and rec.get("ts", 0) < cutoff:
                    continue
                records.append(rec)
        self._cache[profile] = records
        return records

    def count(self, profile: str) -> int:
        return sum(1 for r in self._load(profile) if r.get("t") == "msg")

    def recall(self, profile: str, limit: int = 30) -> list[dict[str, Any]]:
        """取档案里最近 limit 条消息（时间正序）。"""
        msgs = [r for r in self._load(profile) if r.get("t") == "msg"]
        if not self.capture_all:
            msgs = [r for r in msgs if r.get("replied")]
        return msgs[-max(1, limit):]

    def all_profiles(self) -> list[str]:
        return sorted(p.stem for p in self.base.glob("*.jsonl"))

    def clear(self, profile: str) -> None:
        with self._lock:
            path = self.path_for(profile)
            if path.exists():
                path.unlink()
            self._cache.pop(profile, None)

    # ------------------------------------------------------------ 渲染

    @staticmethod
    def render_for_prompt(
        records: list[dict[str, Any]],
        display_names: dict[str, str] | None = None,
        max_chars: int = 6000,
    ) -> str:
        """把档案记录渲染成模型能读的对话记录。

        display_names: person -> 在提示里怎么称呼（用于区分大号/小号）
        """
        names = display_names or {}
        lines: list[str] = []
        for rec in records:
            person = rec.get("person", "?")
            who = names.get(person, person)
            sender = rec.get("sender")
            mtype = rec.get("type") or "text"

            if sender == "system":
                continue
            if sender == "self":
                speaker = "我（本账号）"
            else:
                speaker = who
                if who != person:
                    speaker = f"{who}（用「{person}」这个号）"

            content = str(rec.get("content", "")).replace("\n", " ").strip()
            if not content:
                continue
            if mtype != "text":
                content = f"[{mtype}] {content}"
            lines.append(f"{speaker}: {content}")

        # 从最旧的开始丢弃，保留最近的内容
        while lines and sum(len(x) + 1 for x in lines) > max_chars:
            lines.pop(0)
        return "\n".join(lines)


def load_profiles(raw: dict[str, Any]) -> tuple[dict[str, str], dict[str, Profile]]:
    """从配置里读出 好友名 -> 档案名 的映射。

    支持两种写法：

    1) 简单写法（每个好友独立档案）：
        "好友列表": ["张三", "李四"]

    2) 分组写法（多个号共用一份记录）：
        "好友列表": [
            {"名字": "张三", "档案": "张三", "备注": "大号"},
            {"名字": "张三小号", "档案": "张三", "备注": "小号，和上面共用记录"}
        ]
    """
    mapping: dict[str, str] = {}
    profiles: dict[str, Profile] = {}

    entries = raw.get("好友列表")
    if entries is None:
        # 兼容旧配置：好友白名单.只回复这些好友
        legacy = raw.get("好友白名单", {}).get("只回复这些好友", [])
        entries = legacy if isinstance(legacy, list) else []

    for entry in entries:
        if isinstance(entry, str):
            name = entry.strip()
            if not name:
                continue
            profile = name
            note = ""
        elif isinstance(entry, dict):
            name = str(entry.get("名字", "")).strip()
            if not name:
                continue
            profile = str(entry.get("档案", "")).strip() or name
            note = str(entry.get("备注", "")).strip()
        else:
            continue

        mapping[name] = profile
        prof = profiles.setdefault(profile, Profile(name=profile))
        if name not in prof.members:
            prof.members.append(name)
        if note and not prof.note:
            prof.note = note

    return mapping, profiles
