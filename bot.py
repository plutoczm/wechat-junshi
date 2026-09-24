#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信恋爱军师自动回复 —— 只用 goutoujunshi skill 回答指定好友。

架构：
    轮询会话列表（wxauto4 + UI Automation 驱动微信桌面客户端）
        -> 好友白名单过滤
        -> 对话归档（把读到的消息写进本地档案）
        -> 按档案召回上下文
        -> DeepSeek API（军师 skill 完整规范 + 按需检索参考知识 + 对话记录）
        -> 输出校验（JSON 结构 + 拦截顾问口吻）
        -> 回复队列（模拟真人节奏，支持连发合并）
        -> 校验目标会话后发送

只回复列表里的好友，群聊和其他人一律不处理。

用法：
    启动   python bot.py
    停止   Ctrl+C
    自检   python bot.py --check
    试跑   python bot.py --test "她三天没回我消息了，我该怎么回"
    交互   python bot.py --interactive
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from archive import Archive, load_profiles, msg_to_record
from junshi import HistoryStore, JunshiPrompt, needs_no_reply, parse_model_output

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "data" / "history.json"
ARCHIVE_DIR = BASE_DIR / "data" / "archive"
LOG_DIR = BASE_DIR / "logs"

# 从 DSH 凭据库自动读取 DeepSeek 密钥（可选）。
# 优先用环境变量 DEEPSEEK_API_KEY；找不到再尝试 DSH 桌面版的凭据文件。
DSH_CREDENTIALS = Path(
    os.environ.get(
        "DSH_CREDENTIALS_PATH",
        str(Path(os.environ.get("APPDATA", Path.home())) / "dsh-desktop" / "harness" / ".credentials.yaml"),
    )
)

log = logging.getLogger("junshi")


# ------------------------------------------------------------------ 配置


@dataclass
class Config:
    enabled: bool
    whitelist: list[str]
    profiles: dict[str, str]
    model: str
    api_base: str
    temperature: float
    max_reply_chars: int
    delay_range: tuple[float, float]
    min_interval: float
    max_send_chars: int
    context_turns: int
    text_only: bool
    skip_self: bool
    log_file: str
    memory_enabled: bool
    poll_interval: float
    use_unread_hint: bool
    max_messages_per_poll: int
    only_new_after_start: bool
    # 对话记录归档
    archive_enabled: bool
    archive_capture_all: bool
    archive_recall: int
    archive_keep_days: int
    merge_burst: bool
    merge_window: float

    def profile_of(self, friend: str) -> str:
        """好友名 -> 档案名。没配置就用自己的名字（= 独立记录）。"""
        return self.profiles.get(friend, friend)

    @staticmethod
    def load(path: Path) -> "Config":
        if not path.exists():
            example = path.parent / "config.example.json"
            hint = (
                f"找不到配置文件：{path}\n"
                f"请复制示例配置后修改：\n"
                f'    copy "{example.name}" config.json\n'
            )
            raise FileNotFoundError(hint)

        raw = json.loads(path.read_text(encoding="utf-8"))

        def section(key: str) -> dict:
            val = raw.get(key, {})
            return val if isinstance(val, dict) else {}

        sw = section("启用开关")
        ai = section("AI")
        beh = section("回复行为")
        saf = section("安全")
        mem = section("长期记忆")
        pol = section("轮询")
        arc = section("对话记录")

        # 好友列表：优先新格式，回退旧的 好友白名单
        friends = section("好友列表")
        if friends.get("列表"):
            profiles_map, _ = load_profiles({"好友列表": friends.get("列表", [])})
        else:
            legacy = section("好友白名单").get("只回复这些好友", [])
            profiles_map, _ = load_profiles({"好友白名单": {"只回复这些好友": legacy}})

        delay = beh.get("回复前延迟秒数范围", [2, 5])
        if not (isinstance(delay, list) and len(delay) == 2):
            delay = [2, 5]

        return Config(
            enabled=bool(sw.get("自动回复", False)),
            whitelist=list(profiles_map.keys()),
            profiles=profiles_map,
            model=str(ai.get("模型", "deepseek-chat")),
            api_base=str(ai.get("接口地址", "https://api.deepseek.com")).rstrip("/"),
            temperature=float(ai.get("温度", 1.0)),
            max_reply_chars=int(ai.get("最大回复字数", 800)),
            delay_range=(float(delay[0]), float(delay[1])),
            min_interval=float(beh.get("同一好友最短回复间隔秒", 20)),
            max_send_chars=int(beh.get("单条消息最大发送字数", 1200)),
            context_turns=int(beh.get("上下文轮数", 12)),
            text_only=bool(beh.get("只处理文本消息", True)),
            skip_self=bool(saf.get("忽略自己发的消息", True)),
            log_file=str(saf.get("日志文件", "logs\\bot.log")),
            memory_enabled=bool(mem.get("启用", False)),
            poll_interval=max(2.0, float(pol.get("轮询间隔秒", 5))),
            use_unread_hint=bool(pol.get("使用会话列表未读判断", True)),
            max_messages_per_poll=max(1, int(pol.get("单轮最多处理消息数", 3))),
            only_new_after_start=bool(pol.get("只处理比启动时间新的消息", True)),
            archive_enabled=bool(arc.get("启用归档", True)),
            archive_capture_all=bool(arc.get("归档全部读到的消息", True)),
            archive_recall=max(2, int(arc.get("召回条数", 30))),
            archive_keep_days=max(0, int(arc.get("保留天数", 0))),
            merge_burst=bool(arc.get("合并连发消息", True)),
            merge_window=max(1.0, float(arc.get("连发合并窗口秒", 6))),
        )


def ensure_wechat_window_visible() -> bool:
    """把微信主窗口调出来。

    微信 4.x 只在主窗口显示时才发布 UIA 控件树；最小化到托盘后 wxauto4
    会找不到主窗口。返回是否找到了窗口。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                import psutil

                name = psutil.Process(pid.value).name().lower()
            except Exception:
                return True
            if name.startswith("weixin"):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value.startswith("Qt") and "QWindowIcon" in buf.value:
                    found.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        if not found:
            return False

        hwnd = found[0]
        SW_SHOW, SW_RESTORE = 5, 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.ShowWindow(hwnd, SW_SHOW)
        time.sleep(0.6)
        return True
    except Exception as exc:
        log.debug("调出微信窗口失败：%s", exc)
        return False


def load_api_key() -> str | None:
    """优先环境变量，其次 DSH 桌面版凭据库（可选）。"""
    for env_name in ("DEEPSEEK_API_KEY", "DS_KEY"):
        val = os.environ.get(env_name)
        if val:
            return val.strip()
    if not DSH_CREDENTIALS.exists():
        return None
    text = DSH_CREDENTIALS.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def force_utf8_console() -> None:
    """Windows 控制台默认 GBK，中文和符号会报 UnicodeEncodeError，这里强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(log_file: str | None) -> None:
    force_utf8_console()

    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    log.addHandler(stream_handler)

    if log_file:
        target = BASE_DIR / log_file
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(target, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)


# ------------------------------------------------------------------ AI 调用


class JunshiAI:
    """把一批好友消息变成可发送的微信回复。

    对话上下文来自本地档案（archive），不是模型自带的记忆：
    每次回复前召回该档案最近 N 条记录，渲染成对话记录放进系统提示。
    同一个人的多个号如果指向同一档案，就会共享上下文。
    """

    def __init__(
        self,
        cfg: Config,
        api_key: str,
        skill_dir: Path | None = None,
        archive: Archive | None = None,
    ) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self.prompt = JunshiPrompt(skill_dir) if skill_dir else JunshiPrompt()
        self.archive = archive
        # 兜底用的滚动记录（归档关闭时仍然有上下文）
        self.history = HistoryStore(HISTORY_PATH, turns=cfg.context_turns)
        self._lock = threading.Lock()

    def reply_for(
        self,
        friend: str,
        messages: list[str],
        profile: str | None = None,
    ) -> list[str]:
        """messages 是本次要回应的消息（可能多条连发）。

        返回要发送的消息列表；空列表表示不回复。
        """
        # 全部都是没信息量的短消息 → 直接不回，省一次 API 调用
        if all(needs_no_reply(m) for m in messages):
            log.info("[%s] 消息无信息量（%s），不回复", friend, "、".join(m[:10] for m in messages))
            return []

        prof = profile or self.cfg.profile_of(friend)

        # 本次要回应的内容：多条连发合成一段，逐条列出便于模型全部回应
        if len(messages) == 1:
            current = messages[0]
        else:
            current = "\n".join(f"（第 {i} 条）{m}" for i, m in enumerate(messages, 1))

        system_prompt = self.prompt.build_system_prompt(current)

        # 召回档案里的历史对话
        history_text = ""
        if self.archive is not None and self.cfg.archive_enabled:
            records = self.archive.recall(prof, self.cfg.archive_recall)
            if records:
                # 档案里可能包含多个号（大号/小号），用备注区分
                display = {}
                for name in self.cfg.whitelist:
                    p = self.cfg.profile_of(name)
                    if p == prof:
                        display[name] = name
                history_text = Archive.render_for_prompt(records, display)

        who_note = f"你正在回复微信好友「{friend}」"
        others = [n for n in self.cfg.whitelist if self.cfg.profile_of(n) == prof and n != friend]
        if others:
            who_note += (
                f"。注意：「{'、'.join(others)}」是同一个人使用的另一个微信号，"
                f"和这一个共用同一份对话记录，要当成同一个人来对待"
            )

        system_prompt += f"\n\n## 当前会话\n{who_note}。"

        if history_text:
            system_prompt += (
                "\n\n## 已有的对话记录（按时间先后，最后一条最接近现在）\n"
                f"{history_text}\n\n"
                "请基于这些记录理解上下文：他们之前聊过什么、关系进展到哪一步、"
                "有没有没接住的话题。不要在回复里复述这些记录。"
            )

        model_messages = [{"role": "system", "content": system_prompt}]

        # 归档关闭时用内存滚动记录兜底
        if not history_text:
            with self._lock:
                past = self.history.get(prof)
            model_messages.extend(past[-self.cfg.context_turns * 2:])

        model_messages.append({"role": "user", "content": current})

        raw = self._generate_with_retry(friend, model_messages)
        if raw is None:
            return []

        chunks, ok = parse_model_output(raw, self.cfg.max_reply_chars)
        if not ok:
            log.error("[%s] 重试后仍不合格，本条不回复", friend)
            return []

        if not chunks:
            log.info("[%s] 判定无需回复（skip）", friend)
            self._remember(prof, messages, None)
            return []

        log.info("[%s] 收到：%s", friend, " ｜ ".join(m[:60] for m in messages))
        log.info("[%s] 回复 %d 条：%s", friend, len(chunks), " ｜ ".join(c[:60] for c in chunks))
        self._remember(prof, messages, " ".join(chunks))
        return chunks

    def _generate_with_retry(self, friend: str, messages: list[dict[str, str]]) -> str | None:
        """调用模型；输出被判定为"顾问口吻"时带纠正提示重试。"""
        MAX_ATTEMPTS = 3
        working = list(messages)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            raw = self._call_model(working)
            if raw is None:
                return None

            chunks, ok = parse_model_output(raw, self.cfg.max_reply_chars)
            if ok:
                return raw

            log.warning(
                "[%s] 第 %d 次输出不合格（像在给用户下指令，不是消息），重试",
                friend,
                attempt,
            )
            log.warning("[%s] 不合格的输出：%s", friend, raw[:200].replace("\n", " "))
            if attempt < MAX_ATTEMPTS:
                working = working + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "你刚才输出的内容里包含说给用户听的策略或建议，"
                            "那不是能直接发给对方的消息。请重新只输出 JSON，"
                            "messages 里每一句都必须像真人会直接发出去的微信内容。"
                        ),
                    },
                ]
        return None

    def _call_model(self, messages: list[dict[str, str]]) -> str | None:
        """调用 DeepSeek，返回原始文本；失败返回 None。"""
        try:
            resp = httpx.post(
                f"{self.cfg.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.cfg.model,
                    "messages": messages,
                    "temperature": self.cfg.temperature,
                    "max_tokens": max(512, self.cfg.max_reply_chars * 2),
                    "stream": False,
                },
                timeout=120,
            )
        except httpx.HTTPError as exc:
            log.error("调用 DeepSeek 失败：%s", exc)
            return None

        if resp.status_code != 200:
            log.error("DeepSeek 返回 %s：%s", resp.status_code, resp.text[:300])
            return None

        try:
            payload = resp.json()
            choice = payload["choices"][0]["message"]
            return choice.get("content") or ""
        except (KeyError, IndexError, ValueError) as exc:
            log.error("解析响应失败：%s", exc)
            return None

    def _remember(self, profile: str, user_msgs: list[str], assistant_msg: str | None) -> None:
        """兜底记录（归档关闭时仍能保持上下文）。"""
        with self._lock:
            for m in user_msgs:
                self.history.append(profile, "user", m)
            if assistant_msg:
                self.history.append(profile, "assistant", assistant_msg)
            self.history.save()


# ------------------------------------------------------------------ 主程序


class JunshiBot:
    """轮询式自动回复。

    为什么不用 wxauto4 的 AddListenChat：
    免费版 41.1.7 的监听线程内部调用 chat.GetNewMessage()，而 Chat 类在这个版本里
    根本没有这个方法（只有 GetAllMessage），监听一启动就抛 AttributeError。
    PyPI 上只有 41.1.7 这一个版本，所以改成自己轮询。

    消息记录：
    微信不开放聊天记录，LoadMoreCache 也是坏的，UIA 没有可滚动元素，
    所以无法回读历史。改成"边读边存"——每次轮询读到会话内容就写进本地档案。
    同一个人的多个微信号可以把配置里的 档案 写成同一个名字，共享一份记录。
    """

    def __init__(self, cfg: Config, ai: JunshiAI, archive: Archive | None = None) -> None:
        self.cfg = cfg
        self.ai = ai
        self.archive = archive
        self.reply_queue: queue.Queue[tuple[str, str, list[str]]] = queue.Queue()
        self.last_reply_at: dict[str, float] = {}
        self.stop_event = threading.Event()
        self.wx = None
        # 档案 -> 已见过的消息指纹（首次见到只建立基线，不回复历史）
        self.seen: dict[str, set[str]] = {}
        # 发送失败的重试计数：档案 -> 次数
        self.retry_count: dict[str, int] = {}
        # 连发合并缓冲：档案 -> [(friend, content, ts)]
        self.pending: dict[str, list[tuple[str, str, float]]] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------- 微信侧

    def connect(self):
        """连接微信客户端。

        微信 4.x 只在主窗口**显示**时才发布 UIA 控件树；主窗口最小化或收到托盘后
        wxauto4 会报"未找到已登录的客户端主窗口"。所以先尝试把窗口调出来。
        """
        ensure_wechat_window_visible()

        from wxauto4 import WeChat

        log.info("正在连接微信客户端…")
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                wx = WeChat(ads=False)
                me = wx.GetMyInfo()
                log.info("已连接。当前账号：%s", me.get("display_name") or me)
                if not wx.IsOnline():
                    log.warning("微信报告离线状态，请确认客户端已登录。")
                return wx
            except Exception as exc:
                last_error = exc
                log.warning("连接失败（第 %d 次）：%s", attempt, exc)
                ensure_wechat_window_visible()
                time.sleep(2)

        raise RuntimeError(f"无法连接微信客户端：{last_error}")

    @staticmethod
    def fingerprint(msg) -> str:
        """给一条消息算指纹，用来判断是不是已经处理过。"""
        return "|".join(
            [
                str(getattr(msg, "attr", "")),
                str(getattr(msg, "type", "")),
                str(getattr(msg, "sender", "")),
                str(getattr(msg, "content", ""))[:200],
            ]
        )

    def _unread_targets(self) -> list[str]:
        """从会话列表里找出白名单中当前有未读的好友。"""
        if not self.cfg.use_unread_hint:
            return list(self.cfg.whitelist)
        try:
            sessions = self.wx.GetSession()
        except Exception as exc:
            log.debug("读取会话列表失败：%s", exc)
            return []

        targets: list[str] = []
        for s in sessions:
            name = getattr(s, "name", None)
            if name not in self.cfg.whitelist:
                continue
            try:
                isnew = bool(getattr(s, "isnew", False))
                count = int(getattr(s, "new_count", 0) or 0)
            except (TypeError, ValueError):
                isnew, count = True, 0
            if isnew or count > 0:
                targets.append(name)
        return targets

    def _read_chat(self, friend: str) -> list:
        """切到该好友会话并读取消息。切换校验不通过就返回空。

        安全要点：必须用 ChatInfo 确认当前窗口确实是目标好友，否则绝不发送。
        """
        try:
            self.wx.ChatWith(friend)
        except Exception as exc:
            log.warning("[%s] 切换会话失败：%s", friend, exc)
            return []

        time.sleep(1.0)

        try:
            info = self.wx.ChatInfo()
        except Exception as exc:
            log.warning("[%s] 读取会话信息失败：%s", friend, exc)
            return []

        actual = info.get("chat_name")
        if actual != friend:
            log.warning("[%s] 切换校验失败（当前窗口是 %r），跳过", friend, actual)
            return []
        if info.get("chat_type") != "friend":
            log.info("[%s] 不是好友会话（%s），跳过", friend, info.get("chat_type"))
            return []

        try:
            return list(self.wx.GetAllMessage())
        except Exception as exc:
            log.warning("[%s] 读取消息失败：%s", friend, exc)
            return []

    # -------------------------------------------------- 归档

    def _archive_read(self, friend: str, msgs: list, replied: bool = False) -> None:
        """把读到的消息写进该好友对应的档案。"""
        if self.archive is None or not self.cfg.archive_enabled:
            return
        profile = self.cfg.profile_of(friend)
        records = [msg_to_record(friend, m, replied) for m in msgs]
        try:
            written = self.archive.append(profile, records)
            if written:
                log.debug("归档 [%s] %d 条新记录", profile, written)
        except Exception as exc:
            log.warning("归档失败 [%s]：%s", profile, exc)

    # -------------------------------------------------- 轮询

    def poll_once(self) -> None:
        """轮询一轮：读会话 → 归档 → 找出新消息 → 按档案分组 → 交回复队列。"""
        for friend in self._unread_targets():
            if self.stop_event.is_set():
                return

            msgs = self._read_chat(friend)
            if not msgs:
                continue

            profile = self.cfg.profile_of(friend)
            prints = [self.fingerprint(m) for m in msgs]

            # 归档所有读到的消息（含自己发的和未回复的）
            self._archive_read(friend, msgs)

            known = self.seen.get(profile)

            # 第一次见到这个好友：只建立基线，不回复历史消息
            if known is None:
                self.seen[profile] = set(prints)
                log.info(
                    "[%s] 建立消息基线（%d 条），历史消息不回复（已归档）",
                    friend,
                    len(prints),
                )
                continue

            fresh_indices = [i for i, fp in enumerate(prints) if fp not in known]
            self.seen[profile].update(prints)
            if not fresh_indices:
                continue

            for i in fresh_indices:
                msg = msgs[i]

                if self.cfg.skip_self and getattr(msg, "attr", "") == "self":
                    continue
                if self.cfg.text_only and getattr(msg, "type", "") != "text":
                    log.info("[%s] 非文本消息（%s），跳过", friend, getattr(msg, "type", "?"))
                    continue

                content = str(getattr(msg, "content", "") or "").strip()
                if not content:
                    continue

                log.info("[%s] 新消息：%s", friend, content[:80].replace("\n", " "))
                with self._lock:
                    self.pending.setdefault(profile, []).append((friend, content, time.time()))

            self._flush_pending(profile)

    def _flush_pending(self, profile: str) -> None:
        """把缓冲里的连发消息合并成一批，交给回复队列。"""
        with self._lock:
            bucket = self.pending.get(profile)
            if not bucket:
                return

            if self.cfg.merge_burst:
                # 只处理"安静了超过合并窗口"的批次，让连发的消息凑齐
                newest = max(ts for _, _, ts in bucket)
                if time.time() - newest < self.cfg.merge_window:
                    return

            friend = bucket[0][0]
            batch = [content for _, content, _ in bucket]
            self.pending[profile] = []

        # 单轮上限只限制"批次"，不再限制条数（连发要一起回应）
        if len(batch) > 1:
            log.info("[%s] 合并 %d 条连发消息一起回应", friend, len(batch))
        self.reply_queue.put((friend, profile, batch))

    def _flush_all_pending(self) -> None:
        for profile in list(self.pending.keys()):
            self._flush_pending(profile)

    def poller_loop(self) -> None:
        """后台轮询线程。"""
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                self._flush_all_pending()
            except Exception as exc:
                log.exception("轮询异常：%s", exc)
            # 用可中断的等待，保证 Ctrl+C 后能立刻退出
            self.stop_event.wait(self.cfg.poll_interval)

    # -------------------------------------------------- 回复

    def reply_worker(self) -> None:
        """独立线程里生成并发送回复，避免阻塞轮询。"""
        while not self.stop_event.is_set():
            try:
                friend, profile, batch = self.reply_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                last = self.last_reply_at.get(profile, 0.0)
                gap = time.time() - last
                if gap < self.cfg.min_interval:
                    wait = self.cfg.min_interval - gap
                    log.info("[%s] 距上次回复仅 %.0fs，等待 %.0fs", friend, gap, wait)
                    if self.stop_event.wait(wait):
                        break

                delay = random.uniform(*self.cfg.delay_range)
                if self.stop_event.wait(delay):
                    break

                chunks = self.ai.reply_for(friend, batch, profile=profile)
                if not chunks:
                    continue

                sent = self._send_chunks(friend, chunks)
                if sent == 0:
                    # 一条都没发出去（通常是窗口状态问题）。不要静默丢掉，
                    # 放回队列稍后重试，否则好友永远等不到回复。
                    self._requeue(friend, profile, batch)
                else:
                    self.last_reply_at[profile] = time.time()
            except Exception as exc:
                log.exception("[%s] 生成/发送回复失败：%s", friend, exc)
                self._requeue(friend, profile, batch)
            finally:
                self.reply_queue.task_done()

    def _requeue(self, friend: str, profile: str, batch: list[str], max_retries: int = 2) -> None:
        """发送失败时把消息放回队列重试，超过次数就放弃并明确记日志。"""
        tries = self.retry_count.get(profile, 0)
        if tries >= max_retries:
            log.error(
                "[%s] 已重试 %d 次仍未发出，放弃这批消息（内容：%s）",
                friend,
                tries,
                " ｜ ".join(m[:40] for m in batch),
            )
            self.retry_count.pop(profile, None)
            return

        self.retry_count[profile] = tries + 1
        log.warning("[%s] 发送未成功，稍后重试（第 %d 次）", friend, tries + 1)
        time.sleep(3)
        self.reply_queue.put((friend, profile, batch))

    def _send_chunks(self, friend: str, chunks: list[str]) -> int:
        """按顺序发送多条消息，返回成功条数。

        每条发送前都重新校验目标会话，任一条失败就停止（避免错发到别人窗口）。
        """
        sent = 0
        for i, chunk in enumerate(chunks):
            if self.stop_event.is_set():
                log.warning("[%s] 已在发送中途停止，剩余 %d 条未发", friend, len(chunks) - i)
                break
            if self._send(friend, chunk):
                sent += 1
            else:
                log.warning("[%s] 第 %d 条发送失败，停止后续发送", friend, i + 1)
                break
            if i < len(chunks) - 1:
                time.sleep(random.uniform(0.8, 2.0))
        return sent

    def _ensure_target(self, friend: str, attempts: int = 3) -> bool:
        """确保当前窗口就是目标好友；不是就切过去，切换后再次校验。

        为什么需要：wxauto4 的 ChatInfo 在窗口状态不稳定时会返回 chat_name=None；
        用户手动切到别的会话也会导致目标不对。直接放弃会丢回复，
        所以这里主动切回去重试几次。仍然不行才放弃（绝不能发错人）。
        """
        last_seen: Any = None
        for attempt in range(1, attempts + 1):
            try:
                info = self.wx.ChatInfo()
                if info.get("chat_name") == friend:
                    return True
                last_seen = info.get("chat_name")
            except Exception as exc:
                last_seen = f"<读取失败 {type(exc).__name__}>"

            if attempt < attempts:
                log.warning(
                    "[%s] 当前窗口是 %r，正在切回目标会话（第 %d 次）",
                    friend,
                    last_seen,
                    attempt,
                )
                try:
                    self.wx.ChatWith(friend)
                except Exception as exc:
                    log.warning("[%s] 切回失败：%s", friend, exc)
                time.sleep(1.2)

        log.error("[%s] 多次尝试仍无法定位目标会话（最后是 %r），放弃发送", friend, last_seen)
        return False

    def _send(self, friend: str, text: str) -> bool:
        """发送一条消息。发之前确保当前窗口是目标好友，避免发错人。"""
        if not self._ensure_target(friend):
            return False

        try:
            res = self.wx.SendMsg(text, who=friend, exact=True)
            if res:
                log.info("[%s] 已发送：%s", friend, text[:60].replace("\n", " "))
                return True
            detail = res.get("message") if isinstance(res, dict) else res
            log.error("[%s] 发送失败：%s", friend, detail)
            return False
        except Exception as exc:
            log.error("[%s] 发送异常：%s", friend, exc)
            return False

    # -------------------------------------------------- 生命周期

    def start(self) -> None:
        self.wx = self.connect()

        log.info("白名单（%d 人）：%s", len(self.cfg.whitelist), "、".join(self.cfg.whitelist))
        log.info("轮询间隔：%.0f 秒", self.cfg.poll_interval)

        if self.cfg.archive_enabled and self.archive is not None:
            # 打印分组情况，让"多人共用一份记录"一目了然
            groups: dict[str, list[str]] = {}
            for name in self.cfg.whitelist:
                groups.setdefault(self.cfg.profile_of(name), []).append(name)
            for prof, members in groups.items():
                count = self.archive.count(prof)
                if len(members) > 1:
                    log.info(
                        "档案 [%s]：%s 共用一份记录，已有 %d 条",
                        prof,
                        "、".join(members),
                        count,
                    )
                else:
                    log.info("档案 [%s]：%s，已有 %d 条", prof, members[0], count)
        else:
            log.info("归档未启用，上下文只保留本次运行期间的对话")

        worker = threading.Thread(target=self.reply_worker, name="reply-worker", daemon=True)
        worker.start()
        poller = threading.Thread(target=self.poller_loop, name="poller", daemon=True)
        poller.start()

        log.info("已启动轮询监听。按 Ctrl+C 停止。")

    def run_forever(self) -> None:
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("收到中断，正在退出…")
        finally:
            self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        if self.cfg.archive_enabled and self.archive is not None:
            for prof in list(self.seen.keys()):
                try:
                    log.info("档案 [%s] 共 %d 条记录", prof, self.archive.count(prof))
                except Exception:
                    pass
        try:
            self.ai.history.save()
        except Exception:
            pass
        log.info("已停止。")


# ------------------------------------------------------------------ 自检


def self_check(config_path: Path | None = None) -> int:
    force_utf8_console()
    target_config = config_path or CONFIG_PATH
    print("=" * 64)
    print("自检开始")
    print("=" * 64)
    ok = True

    print("\n[1/6] 配置文件")
    if not target_config.exists():
        print(f"  ✗ 缺少配置文件：{target_config}")
        return 1
    cfg = Config.load(target_config)
    print(f"  ✓ 自动回复开关：{cfg.enabled}")
    print(f"  ✓ 好友数：{len(cfg.whitelist)}")
    if not cfg.whitelist:
        print("  ! 好友列表为空——谁都不会被自动回复（这是预期的安全默认值）")
    if not cfg.enabled:
        print("  ! 自动回复为 false——程序会启动但不会回复任何人")

    # 打印分组情况，确认"多人共用一份记录"配置正确
    groups: dict[str, list[str]] = {}
    for name in cfg.whitelist:
        groups.setdefault(cfg.profile_of(name), []).append(name)
    print(f"  ✓ 对话记录分组（{len(groups)} 份）：")
    for prof, members in groups.items():
        if len(members) > 1:
            print(f"      档案 [{prof}] ← {'、'.join(members)}（共用一份记录）")
        else:
            print(f"      档案 [{prof}] ← {members[0]}（独立记录）")

    print("\n[2/6] 对话记录归档")
    if cfg.archive_enabled:
        arc = Archive(ARCHIVE_DIR, keep_days=cfg.archive_keep_days, capture_all=cfg.archive_capture_all)
        print(f"  ✓ 归档目录：{arc.base}")
        print(f"  ✓ 归档范围：{'全部读到的消息' if cfg.archive_capture_all else '只归档回复过的对话'}")
        print(f"  ✓ 召回条数：{cfg.archive_recall}   保留天数：{cfg.archive_keep_days or '永久'}")
        print(f"  ✓ 连发合并：{'开' if cfg.merge_burst else '关'}（窗口 {cfg.merge_window:.0f} 秒）")
        for prof in groups:
            print(f"      档案 [{prof}] 现有 {arc.count(prof)} 条记录")
    else:
        print("  ! 归档已关闭——只能靠本次运行期间的对话做上下文")

    print("\n[3/6] 军师 skill")
    try:
        prompt = JunshiPrompt()
        print(f"  ✓ 已加载 SKILL.md（{len(prompt.base)} 字符）")
        sample = "她三天没回我消息了，我该怎么回"
        picked = prompt.loaded_references(sample)
        print(f"  ✓ 参考检索可用，示例问题命中 {len(picked)} 份：")
        for p in picked:
            print(f"      - {p}")
    except Exception as exc:
        print(f"  ✗ 加载 skill 失败：{exc}")
        ok = False

    print("\n[4/6] DeepSeek 密钥")
    key = load_api_key()
    if key:
        print(f"  ✓ 已获取密钥（{key[:6]}…，{len(key)} 字符）")
    else:
        print("  ✗ 未找到 DEEPSEEK_API_KEY")
        ok = False

    print("\n[5/6] DeepSeek 连通性")
    if key:
        try:
            r = httpx.post(
                f"{cfg.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": cfg.model,
                    "messages": [{"role": "user", "content": "只回复两个字：在的"}],
                    "max_tokens": 16,
                },
                timeout=40,
            )
            if r.status_code == 200:
                body = r.json()
                print(f"  ✓ {cfg.model} 可用，返回：{body['choices'][0]['message']['content']!r}")
            else:
                print(f"  ✗ HTTP {r.status_code}：{r.text[:200]}")
                ok = False
        except Exception as exc:
            print(f"  ✗ 连接失败：{type(exc).__name__} {exc}")
            ok = False

    print("\n[6/6] 微信客户端")
    try:
        from wxauto4 import WeChat

        ensure_wechat_window_visible()
        wx = WeChat(ads=False)
        me = wx.GetMyInfo()
        print(f"  ✓ 已连接微信，账号：{me.get('display_name') or me}")
        online = wx.IsOnline()
        print(f"  {'✓' if online else '✗'} 在线状态：{online}")
        if not online:
            ok = False

        # 确认配置里的好友在微信里能不能找到
        try:
            sessions = {getattr(s, "name", None) for s in wx.GetSession()}
            for name in cfg.whitelist:
                if name in sessions:
                    print(f"      ✓ 好友「{name}」在会话列表中")
                else:
                    print(f"      ! 好友「{name}」当前不在最近会话里（可能需要先手动搜一次）")
        except Exception as exc:
            print(f"      ! 无法读取会话列表：{exc}")
    except Exception as exc:
        print(f"  ✗ 连接微信失败：{type(exc).__name__}: {exc}")
        print("      若提示『未找到已登录的客户端主窗口』，通常是微信版本高于")
        print("      wxauto4 免费版支持上限（4.1.8.107），或微信主窗口被最小化。")
        ok = False

    print("\n" + "=" * 64)
    print("自检结果：" + ("全部通过" if ok else "存在问题，见上面 ✗ 项"))
    print("=" * 64)
    return 0 if ok else 1


def read_console_line(prompt: str) -> str:
    """从控制台读一行，自动识别 GBK / UTF-8，避免中文变乱码。

    真实控制台的编码取决于 chcp 设置（常见 936=GBK，也可能是 65001=UTF-8），
    这里不依赖 sys.stdin.encoding，而是拿原始字节自己判断。
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()

    buf = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else None
    if buf is None:  # 极端情况下退回普通读取
        return input()

    raw = buf.readline()
    if not raw:
        raise EOFError

    for encoding in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(encoding).rstrip("\r\n")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


def interactive(cfg: Config) -> int:
    """交互式试跑：反复输入消息看军师怎么回。不碰微信。

    中文输入由 Python 从控制台读取，避免批处理/cmd 的编码问题。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    key = load_api_key()
    if not key:
        print("找不到 DEEPSEEK_API_KEY，无法试跑。")
        return 1

    ai = JunshiAI(cfg, key)

    print("=" * 60)
    print("  军师试跑（不会碰微信）")
    print("=" * 60)
    print("  输入一条好友可能发来的消息，回车看军师怎么回。")
    print("  直接回车或输入 q 退出。")
    print()

    while True:
        try:
            message = read_console_line("消息> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not message or message.lower() in ("q", "quit", "exit", "退出"):
            break

        # 试跑时不写入历史，避免污染正式对话上下文
        chunks = ai.reply_for("测试好友", [message])
        print()
        if not chunks:
            print("  （军师判定：这条不需要回复）")
        else:
            print("  ── 军师建议这样回 ──")
            for i, chunk in enumerate(chunks, 1):
                prefix = f"  [{i}] " if len(chunks) > 1 else "  "
                for line_no, line in enumerate(chunk.splitlines()):
                    print(prefix + line if line_no == 0 else "      " + line)
            print("  ────────────────────")
        print()

    print("已退出试跑。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="微信恋爱军师自动回复")
    parser.add_argument("--check", action="store_true", help="只做自检，不启动监听")
    parser.add_argument("--test", metavar="消息", help="用一条消息试跑军师，不碰微信")
    parser.add_argument("--interactive", action="store_true", help="交互式试跑，不碰微信")
    parser.add_argument("--config", metavar="路径", help="使用指定的配置文件（默认 config.json）")
    parser.add_argument(
        "--ignore-unread",
        action="store_true",
        help="调试用：不等未读标记，每轮都去读白名单好友的会话（配合测试配置使用）",
    )
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else CONFIG_PATH

    if args.check:
        return self_check(config_path)

    cfg = Config.load(config_path)
    if args.ignore_unread:
        cfg.use_unread_hint = False
    setup_logging(cfg.log_file)

    if args.interactive:
        return interactive(cfg)

    if args.test:
        key = load_api_key()
        if not key:
            print("未找到 DEEPSEEK_API_KEY")
            return 1
        ai = JunshiAI(cfg, key)
        chunks = ai.reply_for("测试好友", [args.test])
        print("\n=== 军师给出的回复 ===")
        for c in chunks:
            print(f"\n{c}")
        if not chunks:
            print("（判定为无需回复）")
        return 0

    if not cfg.enabled:
        log.warning("配置里『自动回复』为 false，程序不会回复任何人。")
        log.warning("改 config.json 里的 启用开关.自动回复 为 true 后重启。")
    if not cfg.whitelist:
        log.warning("好友列表为空，谁都不会被自动回复。先在 config.json 里填好友。")
        return 1

    key = load_api_key()
    if not key:
        log.error("找不到 DEEPSEEK_API_KEY，无法启动。")
        return 1

    archive = None
    if cfg.archive_enabled:
        archive = Archive(
            ARCHIVE_DIR,
            keep_days=cfg.archive_keep_days,
            capture_all=cfg.archive_capture_all,
        )

    ai = JunshiAI(cfg, key, archive=archive)
    bot = JunshiBot(cfg, ai, archive=archive)
    try:
        bot.start()
    except Exception as exc:
        log.error("启动失败：%s: %s", type(exc).__name__, exc)
        return 1
    bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
