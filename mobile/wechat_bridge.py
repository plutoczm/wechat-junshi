"""Optional Windows bridge for WeChat 4.x via the free/open-source wechatauto-replica.

The bridge is intentionally imported lazily. Linux/server-only deployments keep C-mode
working without any Windows automation dependency.
"""
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import tempfile
import threading
import time

from .store import Problem


TYPE_KIND = {
    "文本": "text",
    "动画表情": "sticker",
    "表情": "sticker",
    "图片": "image",
    "语音": "voice",
    "视频": "video",
    "文件": "file",
    "系统消息": "system",
}


def _iso_from_unix(value):
    try:
        value = int(value)
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _safe_error(exc):
    # Do not leak local paths, wxids or raw DB/key details to the phone UI.
    return type(exc).__name__


class WeChatBridge:
    provider = "wechat"

    def __init__(self, data_dir, enabled=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.enabled = (
            platform.system() == "Windows"
            if enabled is None
            else bool(enabled)
        )
        self._db = None
        self._gui = None
        self._lock = threading.RLock()
        self._last_error = ""
        self._version = ""
        self._owner = ""
        self._status_cache = None
        self._status_cache_at = 0.0
        if self.enabled:
            os.environ.setdefault("WECHATAUTO_RHYTHM", "calm")
            try:
                self._version = importlib.metadata.version("wechatauto-replica")
            except importlib.metadata.PackageNotFoundError:
                self._version = ""

    @property
    def installed(self):
        return bool(self._version)

    def _connect(self):
        if not self.enabled:
            raise Problem("WeChat bridge is disabled on this host", 409)
        if not self.installed:
            raise Problem("Install requirements-wechat.txt on the Windows host", 503)
        with self._lock:
            if self._db is not None:
                return self._db
            try:
                from wechatauto import WeChatDB
                self._db = WeChatDB()
                info = self._db.get_self_info()
                self._owner = str(info.get("username") or self._db.wxid)
                self._last_error = ""
                return self._db
            except Exception as exc:
                self._db = None
                self._last_error = _safe_error(exc)
                raise Problem("WeChat desktop is not ready; keep it logged in and unlocked", 503) from exc

    def reset(self):
        with self._lock:
            self._db = None
            self._gui = None
            self._owner = ""
            self._status_cache = None
            self._status_cache_at = 0.0

    def status(self, force=False):
        now_mono = time.monotonic()
        if not force and self._status_cache and now_mono - self._status_cache_at < 10:
            return dict(self._status_cache)
        result = {
            "enabled": self.enabled,
            "installed": self.installed,
            "package_version": self._version,
            "connected": False,
            "owner_external_id": self._owner,
            "can_read_history": False,
            "can_listen": False,
            "can_send": False,
            "verified_client": "4.1.13.65",
            "error": self._last_error,
        }
        if not self.enabled or not self.installed:
            self._status_cache, self._status_cache_at = dict(result), now_mono
            return result
        try:
            db = self._connect()
            result.update({
                "connected": bool(db),
                "owner_external_id": self._owner,
                "can_read_history": True,
                "can_listen": True,
                "can_send": True,
                "error": "",
            })
        except Problem:
            pass
        self._status_cache, self._status_cache_at = dict(result), now_mono
        return result

    def owner(self):
        self._connect()
        return self._owner

    def search_contacts(self, query):
        query = (query or "").strip()
        if not query:
            raise Problem("Enter a contact nickname, remark or WeChat ID")
        db = self._connect()
        out = []
        for row in db.search_contact(query):
            username = str(row.get("username") or "")
            if not username or username.endswith("@chatroom") or username == self._owner:
                continue
            display = str(row.get("remark") or row.get("nick_name") or username)
            out.append({
                "external_id": username,
                "display_name": display,
                "nick_name": str(row.get("nick_name") or ""),
                "remark": str(row.get("remark") or ""),
                "owner_external_id": self._owner,
            })
        return out[:50]

    @staticmethod
    def _source_key(owner, peer, m):
        # Use fields present in BOTH export_history and get_new_messages. Using
        # server_id only for full export would duplicate the same message when the
        # incremental reader later sees it without server_id.
        digest = hashlib.sha256(
            str(m.get("content") or "").encode("utf-8", "replace")
        ).hexdigest()[:20]
        return (
            f"wechat:{owner}:{peer}:seq:{m.get('sort_seq',0)}:"
            f"local:{m.get('local_id',0)}:type:{m.get('type','')}:"
            f"time:{m.get('create_time',0)}:{digest}"
        )[:240]

    def _row(self, peer, m):
        owner = self.owner()
        sender = m.get("sender_id")
        role = "self" if str(sender) == "2" else "friend"
        mtype = str(m.get("type") or "other")
        kind = TYPE_KIND.get(mtype, "other")
        if role == "friend" and kind == "system":
            role = "system"
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            content = f"[{mtype}]"
        content = "".join(
            ch if ord(ch) >= 32 or ch in "\n\r\t" else " " for ch in content
        )
        if len(content) > 20000:
            content = content[:19970] + "\n[内容过长，已截断]"
        return {
            "id": self._source_key(owner, peer, m),
            "role": role,
            "kind": kind,
            "content": content,
            "occurred_at": _iso_from_unix(m.get("create_time")),
        }

    def read_new(self, peer, since_seq):
        db = self._connect()
        raw = db.get_new_messages(peer, since_seq=max(0, int(since_seq or 0)), limit=500)
        rows = [self._row(peer, m) for m in raw]
        max_seq = max([int(m.get("sort_seq") or 0) for m in raw] or [int(since_seq or 0)])
        return rows, raw, max_seq

    def read_all(self, peer):
        """Read one private chat's complete local desktop history.

        wechatauto-replica's export_history path merges all message shards. A temporary
        JSON file is used only inside the private runtime data directory and deleted in
        a finally block; the canonical copy is the workbench SQLite store.
        """
        db = self._connect()
        temp_dir = self.data_dir / "bridge-tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            temp_dir.chmod(0o700)
        except OSError:
            pass
        fd, path = tempfile.mkstemp(prefix="history-", suffix=".json", dir=temp_dir)
        os.close(fd)
        try:
            try:
                Path(path).chmod(0o600)
            except OSError:
                pass
            db.export_history(path, fmt="json", users=[peer])
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            raw = [m for m in payload.get("messages", []) if m.get("chat") == peer]
            rows = [self._row(peer, m) for m in raw]
            max_seq = max([int(m.get("sort_seq") or 0) for m in raw] or [0])
            return rows, raw, max_seq
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def validate_contact(self, peer, expected_display=None):
        db = self._connect()
        current = str(db.get_nickname(peer) or "")
        if not current or current == peer:
            raise Problem("WeChat contact was not found in the local contact database", 404)
        if expected_display is not None and current != expected_display:
            raise Problem("WeChat contact display name changed; refresh search results", 409)
        return {
            "external_id": peer,
            "display_name": current,
            "owner_external_id": self._owner,
        }

    def _safe_send_name(self, peer, expected_display):
        db = self._connect()
        current = str(db.get_nickname(peer) or "")
        if not current or current != expected_display:
            raise Problem(
                "WeChat contact display name changed; refresh the binding before B mode sends",
                409,
            )
        exact = []
        for hit in db.search_contact(current):
            username = str(hit.get("username") or "")
            display = str(hit.get("remark") or hit.get("nick_name") or username)
            if display == current and username and not username.endswith("@chatroom"):
                exact.append(username)
        if exact.count(peer) != 1 or len(set(exact)) != 1:
            raise Problem(
                "The WeChat display name is not unique; B mode refuses to guess a send target",
                409,
            )
        return current

    def send_text(self, peer, display_name, content):
        target = self._safe_send_name(peer, display_name)
        with self._lock:
            try:
                if self._gui is None:
                    from wechatauto.guia import WeChatGUI
                    self._gui = WeChatGUI()
                response = self._gui.send_msg(content, target, verify=True)
            except Exception as exc:
                self._last_error = _safe_error(exc)
                return {"state": "failed", "detail": "bridge exception"}
        if bool(response):
            return {"state": "confirmed", "detail": "database verified"}
        message = str(response.get("message") or "") if isinstance(response, dict) else str(response)
        if "未确认" in message or "已操作发送" in message:
            return {"state": "unknown", "detail": "send operated but database verification was inconclusive"}
        return {"state": "failed", "detail": "send did not complete"}


class NullBridge(WeChatBridge):
    def __init__(self, data_dir=None):
        super().__init__(data_dir or ".", enabled=False)
