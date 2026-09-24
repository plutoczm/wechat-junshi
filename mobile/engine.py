"""Reply generation plus restart-safe WeChat polling/sending orchestration."""
from datetime import datetime, timezone
import threading
import time

from .store import Problem


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


class ReplyEngine:
    def __init__(self, store, model, trends=None):
        self.store = store
        self.model = model
        self.trends = trends
        self._generation_lock = threading.BoundedSemaphore(1)

    def generate(self, account_id, message_ids):
        if not self._generation_lock.acquire(blocking=False):
            raise Problem("Another generation is in progress; try later", 429)
        try:
            snapshot = self.store.context_bundle(
                account_id, message_ids, cloud_only=True
            )
            query = "\n".join(r["content"] for r in snapshot.get("incoming", []))
            snapshot["trends"] = self.trends.search(query) if self.trends else []
            messages, explanation = self.model.generate(snapshot)
            return self.store.save_draft(
                snapshot, messages, explanation, source_message_ids=message_ids
            )
        finally:
            self._generation_lock.release()


class AutomationService:
    """One background thread owns bridge polling and verified B-mode sending.

    It never guesses a target and never retries an "unknown" send. C/B are account
    properties, while retrieval remains person-scoped.
    """

    def __init__(self, store, engine, bridge, poll_interval=1.5, debounce=4.0):
        self.store = store
        self.engine = engine
        self.bridge = bridge
        self.poll_interval = max(0.5, float(poll_interval))
        self.debounce = max(1.0, float(debounce))
        self.stop_event = threading.Event()
        self.thread = None
        self.trend_thread = None
        self.sync_jobs = {}
        self._sync_lock = threading.Lock()
        self._loop_error = ""

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        if not self.bridge.enabled:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="junshi-wechat-bridge", daemon=True)
        self.thread.start()
        if self.engine.trends and self.engine.trends.enabled:
            self.trend_thread = threading.Thread(
                target=self._run_trends, name="junshi-public-trends", daemon=True
            )
            self.trend_thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)
        if self.trend_thread:
            self.trend_thread.join(timeout=2)

    def status(self):
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "last_loop_error": self._loop_error,
        }

    def _run_trends(self):
        while not self.stop_event.is_set():
            try:
                self.engine.trends.refresh()
            except Exception:
                pass
            self.stop_event.wait(max(60, self.engine.trends.ttl))

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                self.process_inbox()
                self.process_outbox()
                self._loop_error = ""
            except Problem as exc:
                self._loop_error = exc.message
                self.bridge.reset()
            except Exception as exc:
                self._loop_error = type(exc).__name__
                self.bridge.reset()
            self.stop_event.wait(self.poll_interval)

    def poll_once(self):
        status = self.bridge.status()
        if not status.get("connected"):
            return
        owner = status.get("owner_external_id")
        for account in self.store.transport_accounts(enabled_only=True):
            if account["owner_external_id"] != owner:
                continue
            rows, raw, max_seq = self.bridge.read_new(
                account["external_id"], account["cursor_seq"]
            )
            if not rows:
                continue
            result = self.store.ingest_transport(account["id"], rows)
            new_set = set(result["new_message_ids"])
            incoming = []
            for row, mid in zip(rows, result["message_ids"]):
                if (
                    mid in new_set and row["role"] == "friend" and row["kind"] == "text"
                    and account["mode"] in ("C", "B")
                ):
                    incoming.append(mid)
            if incoming:
                self.store.enqueue_inbox(account["id"], incoming)
            # Cursor only advances after durable ingestion.
            self.store.update_transport_cursor(account["id"], max_seq)

    def process_inbox(self):
        now_ts = time.time()
        for account in self.store.transport_accounts(enabled_only=True):
            if account["mode"] not in ("C", "B") or not account["cloud"]:
                continue
            pending = self.store.pending_inbox(account["id"], limit=20)
            if not pending:
                continue
            newest = max(_parse_iso(r["created_at"]) for r in pending)
            if now_ts - newest < self.debounce:
                continue
            mids = [r["message_id"] for r in pending]
            try:
                draft = self.engine.generate(account["id"], mids)
            except Problem as exc:
                if exc.status in (409, 429):
                    continue
                raise
            self.store.attach_inbox_draft(mids, draft["id"], "drafted")
            if account["mode"] == "B":
                self.store.queue_outbox(draft["id"])

    def process_outbox(self):
        item = self.store.claim_outbox()
        if not item:
            return
        payload = item["payload"]
        start = int(item["confirmed_count"])
        transport = item["transport"]
        for index in range(start, len(payload)):
            if self.stop_event.is_set():
                self.store.finish_outbox(item["id"], "failed", "service stopped before send")
                return
            result = self.bridge.send_text(
                transport["external_id"], transport["display_name"], payload[index]
            )
            if result["state"] == "confirmed":
                self.store.confirm_outbox_chunk(item["id"], index, payload[index])
                continue
            if result["state"] == "unknown":
                self.store.finish_outbox(item["id"], "unknown", result.get("detail", ""))
            else:
                self.store.finish_outbox(item["id"], "failed", result.get("detail", ""))
            return
        self.store.finish_outbox(item["id"], "confirmed")

    def start_sync(self, account_id):
        account = self.store.transport_account(account_id)
        if not self.bridge.status().get("can_read_history"):
            raise Problem("WeChat history bridge is not ready", 503)
        with self._sync_lock:
            old = self.sync_jobs.get(account_id)
            if old and old.get("state") == "running":
                raise Problem("History sync is already running", 409)
            self.sync_jobs[account_id] = {
                "state": "running", "imported": 0, "total": 0, "error": "",
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        thread = threading.Thread(
            target=self._sync_worker, args=(account_id, account),
            name="junshi-history-sync", daemon=True
        )
        thread.start()
        return dict(self.sync_jobs[account_id])

    def _sync_worker(self, account_id, account):
        try:
            rows, raw, max_seq = self.bridge.read_all(account["external_id"])
            with self._sync_lock:
                self.sync_jobs[account_id]["total"] = len(rows)
            imported = 0
            for start in range(0, len(rows), 1000):
                if self.stop_event.is_set():
                    raise RuntimeError("service stopped")
                batch = rows[start:start + 1000]
                result = self.store.ingest_transport(account_id, batch)
                imported += result["new"]
                with self._sync_lock:
                    self.sync_jobs[account_id]["imported"] = imported
            self.store.update_transport_cursor(account_id, max_seq, synced=True)
            with self._sync_lock:
                self.sync_jobs[account_id].update({
                    "state": "done", "imported": imported, "error": "",
                    "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
        except Exception as exc:
            with self._sync_lock:
                self.sync_jobs[account_id].update({
                    "state": "failed",
                    "error": exc.message if isinstance(exc, Problem) else type(exc).__name__,
                    "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })

    def sync_status(self, account_id):
        with self._sync_lock:
            return dict(self.sync_jobs.get(account_id) or {"state": "idle"})

    def retry_outbox(self, outbox_id):
        self.store.retry_outbox(outbox_id)
