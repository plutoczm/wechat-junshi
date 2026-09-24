"""Fresh public Chinese trend phrases, fetched without sending private chat text.

Default provider is the open-source DailyHotApi-compatible public endpoint. Set
JUNSHI_TRENDS_BASE to a self-hosted instance, or to an empty string to disable it.
"""
from datetime import datetime, timezone
import os
import threading
import time
from urllib.parse import urljoin

import httpx

from .store import terms


DEFAULT_SOURCES = ("weibo", "douyin", "bilibili", "zhihu", "tieba", "baidu", "toutiao", "kuaishou")


class Trends:
    def __init__(self, base=None, sources=None, ttl=300, transport=None):
        self.base = (
            os.environ.get("JUNSHI_TRENDS_BASE", "https://api-hot.lhzzs.top/")
            if base is None else base
        ).strip()
        configured = os.environ.get("JUNSHI_TREND_SOURCES", "")
        if sources is None and configured:
            sources = tuple(x.strip() for x in configured.split(",") if x.strip())
        self.sources = tuple(sources or DEFAULT_SOURCES)
        self.ttl = max(60, int(ttl))
        self.transport = transport
        self._lock = threading.Lock()
        self._cache = {}
        self._last_error = ""
        self._last_refresh = ""

    @property
    def enabled(self):
        return bool(self.base)

    def _fetch(self, source):
        now_mono = time.monotonic()
        cached = self._cache.get(source)
        if cached and now_mono - cached[0] < self.ttl:
            return cached[1]
        if not self.enabled:
            return []
        url = urljoin(self.base.rstrip("/") + "/", source + "/new")
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=httpx.Timeout(8, connect=4),
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "wechat-junshi/1.0"},
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
            data = payload.get("data", [])
            if not isinstance(data, list):
                data = []
            items = []
            for row in data[:60]:
                if not isinstance(row, dict):
                    continue
                title = row.get("title") or row.get("word") or row.get("name")
                if not isinstance(title, str) or not title.strip():
                    continue
                items.append({
                    "source": source,
                    "title": title.strip()[:160],
                    "updated_at": str(payload.get("updateTime") or "")[:64],
                })
            self._cache[source] = (now_mono, items)
            self._last_refresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._last_error = ""
            return items
        except (httpx.HTTPError, ValueError, TypeError):
            self._last_error = f"{source}:unavailable"
            return cached[1] if cached else []

    def search(self, private_query, limit=8):
        """Local relevance match. private_query is never sent to the trend provider."""
        if not self.enabled:
            return []
        qterms = set(terms(private_query))
        if not qterms:
            return []
        scored = []
        with self._lock:
            for source in self.sources:
                for item in self._fetch(source):
                    tterms = set(terms(item["title"]))
                    overlap = len(qterms & tterms)
                    if overlap:
                        scored.append((overlap, item))
        scored.sort(key=lambda x: (-x[0], x[1]["source"], x[1]["title"]))
        seen, out = set(), []
        for score, item in scored:
            key = item["title"]
            if key in seen:
                continue
            seen.add(key)
            out.append({**item, "relevance_terms": score})
            if len(out) >= max(1, min(int(limit), 12)):
                break
        return out

    def status(self):
        return {
            "enabled": self.enabled,
            "provider": "DailyHotApi-compatible",
            "sources": list(self.sources),
            "cache_seconds": self.ttl,
            "last_refresh": self._last_refresh,
            "last_error": self._last_error,
            "privacy": "private chat text is matched locally and is never sent to the trend endpoint",
        }
