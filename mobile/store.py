"""Persistent relationship memory plus transport-safe draft/outbox state."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid


VALID_KINDS = {"text", "sticker", "image", "voice", "video", "file", "system", "other"}
VALID_ROLES = {"friend", "self", "system"}
VALID_MODES = {"OFF", "C", "B"}


class Problem(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid():
    return uuid.uuid4().hex


def terms(value):
    """Portable Chinese bigram / Latin word index; deliberately local-only."""
    words = set(re.findall(r"[a-z0-9]{2,32}", value.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            words.add(run)
    return sorted(words)[:512]


def text(value, maximum=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Problem("Text is empty, invalid, or too long")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise Problem("Control characters are not allowed")
    return value.strip()


def parse_time(value):
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError) as exc:
        raise Problem("occurred_at requires an ISO-8601 timezone, or null") from exc


class Store:
    """Single-owner SQLite store.

    Accounts are evidence sources; persons are the shared-memory boundary. A transport
    binding points one account at one concrete WeChat peer. Transport state is kept
    separate from the relationship identity so merging two WeChat accounts never
    merges their cursors, queues or send targets.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise Problem("Unsupported database version; do not overwrite", 409)
            db.executescript("""
            CREATE TABLE IF NOT EXISTS persons(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS accounts(
                id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES persons(id),
                label TEXT NOT NULL UNIQUE, cloud INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                source_key TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
                content TEXT NOT NULL, occurred_at TEXT, observed_at TEXT NOT NULL,
                origin TEXT NOT NULL, UNIQUE(account_id, source_key));
            CREATE INDEX IF NOT EXISTS msg_account ON messages(account_id, observed_at);
            CREATE TABLE IF NOT EXISTS message_terms(
                term TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                PRIMARY KEY(term, message_id));
            CREATE TABLE IF NOT EXISTS notes(
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                content TEXT NOT NULL, message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts(
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                person_id TEXT NOT NULL, revision INTEGER NOT NULL, messages TEXT NOT NULL,
                explanation TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS account_transport(
                account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
                provider TEXT NOT NULL, owner_external_id TEXT NOT NULL,
                external_id TEXT NOT NULL, display_name TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'C', enabled INTEGER NOT NULL DEFAULT 1,
                cursor_seq INTEGER NOT NULL DEFAULT 0, last_sync_at TEXT,
                UNIQUE(provider, owner_external_id, external_id));
            CREATE TABLE IF NOT EXISTS draft_sources(
                draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                PRIMARY KEY(draft_id, message_id));
            CREATE TABLE IF NOT EXISTS inbox(
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
                draft_id TEXT REFERENCES drafts(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS inbox_account_status ON inbox(account_id, status, created_at);
            CREATE TABLE IF NOT EXISTS outbox(
                id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id) ON DELETE CASCADE,
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                person_id TEXT NOT NULL, revision INTEGER NOT NULL,
                payload TEXT NOT NULL, confirmed_count INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state, created_at);
            PRAGMA user_version = 2;
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA secure_delete = ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def account(db, account_id):
        row = db.execute(
            "SELECT a.*,p.revision FROM accounts a JOIN persons p ON p.id=a.person_id WHERE a.id=?",
            (account_id,),
        ).fetchone()
        if not row:
            raise Problem("Account not found", 404)
        return dict(row)

    @staticmethod
    def transport_row(db, account_id, required=False):
        row = db.execute("SELECT * FROM account_transport WHERE account_id=?", (account_id,)).fetchone()
        if required and not row:
            raise Problem("This account is not linked to a WeChat contact", 409)
        return dict(row) if row else None

    @staticmethod
    def _purge_draft_payload(db, person_id):
        rows = db.execute(
            "SELECT id FROM drafts WHERE person_id=? AND status IN ('pending','queued')",
            (person_id,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            marks = ",".join("?" for _ in ids)
            db.execute(
                f"UPDATE drafts SET status='stale',messages='[]',explanation='',evidence='[]' WHERE id IN ({marks})",
                ids,
            )
            db.execute(f"DELETE FROM draft_sources WHERE draft_id IN ({marks})", ids)
            db.execute(
                f"UPDATE inbox SET draft_id=NULL,status='new',updated_at=? WHERE draft_id IN ({marks})",
                [now(), *ids],
            )
            db.execute(
                f"UPDATE outbox SET state='cancelled',payload='[]',last_error='context changed',updated_at=? "
                f"WHERE draft_id IN ({marks}) AND state='queued'",
                [now(), *ids],
            )

    @classmethod
    def touch(cls, db, person_ids):
        for person in set(p for p in person_ids if p):
            db.execute("UPDATE persons SET revision=revision+1 WHERE id=?", (person,))
            cls._purge_draft_payload(db, person)

    def state(self):
        with self.db() as db:
            people = [dict(r) for r in db.execute("SELECT * FROM persons ORDER BY name,id")]
            for p in people:
                rows = db.execute(
                    """SELECT a.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.account_id=a.id) AS message_count,
                       t.provider,t.owner_external_id,t.external_id,t.display_name,
                       t.mode,t.enabled,t.cursor_seq,t.last_sync_at
                       FROM accounts a LEFT JOIN account_transport t ON t.account_id=a.id
                       WHERE a.person_id=? ORDER BY a.label""",
                    (p["id"],),
                )
                p["accounts"] = [dict(r) for r in rows]
            return people

    def add_person(self, name):
        name = text(name, 80)
        with self.db() as db:
            result = uid()
            db.execute("INSERT INTO persons(id,name) VALUES(?,?)", (result, name))
        return result

    def add_account(self, person_id, label):
        label = text(label, 80)
        with self.db() as db:
            if not db.execute("SELECT 1 FROM persons WHERE id=?", (person_id,)).fetchone():
                raise Problem("Person not found", 404)
            result = uid()
            try:
                db.execute("INSERT INTO accounts(id,person_id,label) VALUES(?,?,?)", (result, person_id, label))
            except sqlite3.IntegrityError as exc:
                raise Problem("Use a unique account label", 409) from exc
            self.touch(db, [person_id])
        return result

    def configure(self, account_id, cloud, mode=None):
        if not isinstance(cloud, bool):
            raise Problem("cloud must be a boolean")
        if mode is not None and mode not in VALID_MODES:
            raise Problem("mode must be OFF, C or B")
        with self.db() as db:
            a = self.account(db, account_id)
            db.execute("UPDATE accounts SET cloud=? WHERE id=?", (int(cloud), account_id))
            if mode is not None:
                t = self.transport_row(db, account_id, required=(mode == "B"))
                if t:
                    db.execute("UPDATE account_transport SET mode=? WHERE account_id=?", (mode, account_id))
                elif mode not in ("C", "OFF"):
                    raise Problem("B mode requires a linked WeChat contact", 409)
            self.touch(db, [a["person_id"]])

    def link_transport(self, account_id, owner_external_id, external_id, display_name):
        owner_external_id = text(owner_external_id, 160)
        external_id = text(external_id, 160)
        display_name = text(display_name, 160)
        if external_id.endswith("@chatroom"):
            raise Problem("Group chats are not supported by this project", 409)
        with self.db() as db:
            a = self.account(db, account_id)
            try:
                db.execute(
                    """INSERT INTO account_transport(
                       account_id,provider,owner_external_id,external_id,display_name,mode,enabled,cursor_seq)
                       VALUES(?,?,?,?,?,'C',1,0)
                       ON CONFLICT(account_id) DO UPDATE SET provider='wechat',
                       owner_external_id=excluded.owner_external_id,
                       external_id=excluded.external_id,display_name=excluded.display_name,
                       mode='C',enabled=1,cursor_seq=0,last_sync_at=NULL""",
                    (account_id, "wechat", owner_external_id, external_id, display_name),
                )
            except sqlite3.IntegrityError as exc:
                raise Problem("That WeChat contact is already linked to another account", 409) from exc
            self.touch(db, [a["person_id"]])

    def unlink_transport(self, account_id):
        with self.db() as db:
            a = self.account(db, account_id)
            db.execute("DELETE FROM account_transport WHERE account_id=?", (account_id,))
            self.touch(db, [a["person_id"]])

    def set_transport_mode(self, account_id, mode, enabled=True):
        if mode not in VALID_MODES or not isinstance(enabled, bool):
            raise Problem("Invalid transport mode")
        with self.db() as db:
            a = self.account(db, account_id)
            self.transport_row(db, account_id, required=True)
            db.execute(
                "UPDATE account_transport SET mode=?,enabled=? WHERE account_id=?",
                (mode, int(enabled), account_id),
            )
            self.touch(db, [a["person_id"]])
            if mode == "OFF" or not enabled:
                db.execute(
                    "UPDATE inbox SET status='dismissed',draft_id=NULL,updated_at=? "
                    "WHERE account_id=? AND status='new'",
                    (now(), account_id),
                )

    def transport_account(self, account_id):
        with self.db() as db:
            a = self.account(db, account_id)
            t = self.transport_row(db, account_id, required=True)
            return {**a, **t}

    def transport_accounts(self, enabled_only=True):
        with self.db() as db:
            sql = """SELECT a.id,a.person_id,a.label,a.cloud,p.revision,
                     t.provider,t.owner_external_id,t.external_id,t.display_name,
                     t.mode,t.enabled,t.cursor_seq,t.last_sync_at
                     FROM accounts a JOIN persons p ON p.id=a.person_id
                     JOIN account_transport t ON t.account_id=a.id
                     WHERE t.provider='wechat'"""
            if enabled_only:
                sql += " AND t.enabled=1"
            return [dict(r) for r in db.execute(sql + " ORDER BY a.label")]

    def find_transport_account(self, owner_external_id, external_id):
        with self.db() as db:
            row = db.execute(
                """SELECT a.id FROM account_transport t JOIN accounts a ON a.id=t.account_id
                   WHERE t.provider='wechat' AND t.owner_external_id=? AND t.external_id=?""",
                (owner_external_id, external_id),
            ).fetchone()
            return row["id"] if row else None

    def update_transport_cursor(self, account_id, seq, synced=False):
        seq = max(0, int(seq or 0))
        with self.db() as db:
            self.transport_row(db, account_id, required=True)
            if synced:
                db.execute(
                    "UPDATE account_transport SET cursor_seq=MAX(cursor_seq,?),last_sync_at=? WHERE account_id=?",
                    (seq, now(), account_id),
                )
            else:
                db.execute(
                    "UPDATE account_transport SET cursor_seq=MAX(cursor_seq,?) WHERE account_id=?",
                    (seq, account_id),
                )

    def move(self, account_ids, person_id):
        if not account_ids or len(set(account_ids)) != len(account_ids):
            raise Problem("Select distinct accounts")
        with self.db() as db:
            if not db.execute("SELECT 1 FROM persons WHERE id=?", (person_id,)).fetchone():
                raise Problem("Person not found", 404)
            accounts = [self.account(db, i) for i in account_ids]
            for a in accounts:
                db.execute("UPDATE accounts SET person_id=? WHERE id=?", (person_id, a["id"]))
            self.touch(db, [person_id] + [a["person_id"] for a in accounts])

    def delete_account(self, account_id):
        with self.db() as db:
            a = self.account(db, account_id)
            db.execute("DELETE FROM drafts WHERE person_id=?", (a["person_id"],))
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self.touch(db, [a["person_id"]])

    @staticmethod
    def normalize(rows, batch_id):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
            raise Problem("Provide 1-5000 messages per batch")
        batch_id = text(batch_id, 128)
        result = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) - {"id", "role", "kind", "content", "occurred_at"}:
                raise Problem("Invalid import fields; use the documented JSONL format")
            role, kind = row.get("role"), row.get("kind", "text")
            if role not in VALID_ROLES or kind not in VALID_KINDS:
                raise Problem("Invalid message role or kind")
            text(row.get("content"))
            content = row["content"]
            timestamp = parse_time(row.get("occurred_at"))
            key = text(row["id"], 240) if row.get("id") is not None else "batch:" + batch_id + ":" + str(i)
            result.append((key, role, kind, content, timestamp))
        if len({r[0] for r in result}) != len(result):
            raise Problem("Duplicate source message IDs inside a batch")
        return result

    @staticmethod
    def _insert_normalized(db, account_id, normalized, origin):
        ids, new_ids, added = [], [], 0
        for key, role, kind, content, occurred in normalized:
            old = db.execute(
                "SELECT * FROM messages WHERE account_id=? AND source_key=?", (account_id, key)
            ).fetchone()
            if old:
                if (old["role"], old["kind"], old["content"], old["occurred_at"]) != (
                    role, kind, content, occurred
                ):
                    raise Problem("Source ID conflict: existing message differs; nothing imported", 409)
                ids.append(old["id"])
                continue
            mid = uid()
            db.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                (mid, account_id, key, role, kind, content, occurred, now(), origin),
            )
            db.executemany("INSERT INTO message_terms VALUES(?,?)", [(t, mid) for t in terms(content)])
            ids.append(mid)
            new_ids.append(mid)
            added += 1
        return ids, new_ids, added

    def ingest(self, account_id, rows, batch_id, confirm=False, expected_digest=None, origin="import"):
        normalized = self.normalize(rows, batch_id)
        digest = hashlib.sha256(
            json.dumps([account_id, normalized], ensure_ascii=False).encode()
        ).hexdigest()
        if confirm and expected_digest != digest:
            raise Problem("Preview changed; preview again before importing", 409)
        ids, new_ids, added = [], [], 0
        with self.db() as db:
            a = self.account(db, account_id)
            if confirm:
                ids, new_ids, added = self._insert_normalized(db, account_id, normalized, origin)
                if added:
                    self.touch(db, [a["person_id"]])
            else:
                for key, role, kind, content, occurred in normalized:
                    old = db.execute(
                        "SELECT * FROM messages WHERE account_id=? AND source_key=?", (account_id, key)
                    ).fetchone()
                    if old:
                        if (old["role"], old["kind"], old["content"], old["occurred_at"]) != (
                            role, kind, content, occurred
                        ):
                            raise Problem("Source ID conflict: existing message differs; nothing imported", 409)
                    else:
                        added += 1
        return {
            "digest": digest, "total": len(rows), "new": added,
            "duplicates": len(rows) - added, "message_ids": ids, "new_message_ids": new_ids,
        }

    def ingest_transport(self, account_id, rows, origin="wechat_db"):
        normalized = self.normalize(rows, "transport")
        with self.db() as db:
            a = self.account(db, account_id)
            self.transport_row(db, account_id, required=True)
            ids, new_ids, added = self._insert_normalized(db, account_id, normalized, origin)
            if added:
                self.touch(db, [a["person_id"]])
            return {"message_ids": ids, "new_message_ids": new_ids, "new": added}

    def add_note(self, account_id, content, message_id=None):
        content = text(content, 1000)
        with self.db() as db:
            a = self.account(db, account_id)
            if message_id and not db.execute(
                "SELECT 1 FROM messages WHERE id=? AND account_id=?", (message_id, account_id)
            ).fetchone():
                raise Problem("Evidence must belong to the selected account")
            result = uid()
            db.execute("INSERT INTO notes VALUES(?,?,?,?,?)", (result, account_id, content, message_id, now()))
            self.touch(db, [a["person_id"]])
        return result

    def delete_note(self, account_id, note_id):
        with self.db() as db:
            a = self.account(db, account_id)
            db.execute("DELETE FROM notes WHERE id=? AND account_id=?", (note_id, account_id))
            self.touch(db, [a["person_id"]])

    def context(self, account_id, query="", cloud_only=False, message_id=None):
        with self.db() as db:
            a = self.account(db, account_id)
            if cloud_only and not a["cloud"]:
                raise Problem("Enable text disclosure to DeepSeek for this account first", 403)
            gate = " AND a.cloud=1" if cloud_only else ""
            scope = " FROM messages m JOIN accounts a ON a.id=m.account_id WHERE a.person_id=?" + gate
            args = [a["person_id"]]
            if cloud_only:
                scope += " AND m.kind='text' AND m.role IN ('self','friend')"
            recent = [
                dict(r) for r in db.execute(
                    "SELECT m.*" + scope +
                    " ORDER BY COALESCE(m.occurred_at,m.observed_at) DESC,m.rowid DESC LIMIT 24",
                    args,
                )
            ]
            keywords = terms(query)[:40]
            matches = []
            if keywords:
                marks = ",".join("?" for _ in keywords)
                sql = (
                    "SELECT m.*,COUNT(*) AS score FROM message_terms t "
                    "JOIN messages m ON m.id=t.message_id "
                    "JOIN accounts a ON a.id=m.account_id WHERE a.person_id=?" + gate
                )
                if cloud_only:
                    sql += " AND m.kind='text' AND m.role IN ('self','friend')"
                sql += (
                    f" AND t.term IN ({marks}) GROUP BY m.id "
                    "ORDER BY score DESC,COALESCE(m.occurred_at,m.observed_at) DESC LIMIT 16"
                )
                matches = [dict(r) for r in db.execute(sql, args + keywords)]
            notes = [
                dict(r) for r in db.execute(
                    "SELECT n.* FROM notes n JOIN accounts a ON a.id=n.account_id "
                    "WHERE a.person_id=?" + gate + " ORDER BY n.created_at DESC LIMIT 30", args
                )
            ]
            current = None
            if message_id:
                row = db.execute(
                    "SELECT * FROM messages WHERE id=? AND account_id=? AND role='friend' AND kind='text'",
                    (message_id, account_id),
                ).fetchone()
                if not row:
                    raise Problem("Choose a text message from this account", 404)
                current = dict(row)
            combined = {r["id"]: r for r in matches + recent}
            records = sorted(
                combined.values(),
                key=lambda r: (r["occurred_at"] or r["observed_at"], r["id"]),
            )
            return {
                "person_id": a["person_id"], "revision": a["revision"],
                "account_id": account_id, "records": records, "notes": notes, "current": current,
            }

    def context_bundle(self, account_id, message_ids, cloud_only=True):
        if not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 20:
            raise Problem("Select 1-20 incoming messages")
        with self.db() as db:
            a = self.account(db, account_id)
            if cloud_only and not a["cloud"]:
                raise Problem("Enable text disclosure to DeepSeek for this account first", 403)
            marks = ",".join("?" for _ in message_ids)
            rows = db.execute(
                f"SELECT * FROM messages WHERE account_id=? AND id IN ({marks}) "
                "AND role='friend' AND kind='text'",
                [account_id, *message_ids],
            ).fetchall()
            by_id = {r["id"]: dict(r) for r in rows}
        if len(by_id) != len(set(message_ids)):
            raise Problem("Incoming message set changed; refresh and try again", 409)
        incoming = [by_id[i] for i in message_ids]
        query = "\n".join(r["content"] for r in incoming)
        base = self.context(account_id, query, cloud_only=cloud_only, message_id=message_ids[-1])
        base["incoming"] = incoming
        return base

    def save_draft(self, snapshot, messages, explanation, source_message_ids=None):
        if not isinstance(messages, list) or len(messages) > 3:
            raise Problem("Invalid draft messages")
        values = [text(m, 1000) for m in messages]
        if not isinstance(explanation, str) or len(explanation) > 1000:
            raise Problem("Invalid draft explanation")
        sources = source_message_ids or (
            [snapshot["current"]["id"]] if snapshot.get("current") else []
        )
        with self.db() as db:
            a = self.account(db, snapshot["account_id"])
            if (
                not a["cloud"] or a["person_id"] != snapshot["person_id"]
                or a["revision"] != snapshot["revision"]
            ):
                raise Problem("Context or consent changed while generating; generate again", 409)
            if sources:
                marks = ",".join("?" for _ in sources)
                count = db.execute(
                    f"SELECT COUNT(*) FROM messages WHERE account_id=? AND id IN ({marks})",
                    [a["id"], *sources],
                ).fetchone()[0]
                if count != len(set(sources)):
                    raise Problem("Draft source messages changed", 409)
            did = uid()
            evidence = [r["id"] for r in snapshot["records"]]
            db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    did, a["id"], a["person_id"], a["revision"],
                    json.dumps(values, ensure_ascii=False), explanation,
                    json.dumps(evidence), "pending", now(),
                ),
            )
            db.executemany(
                "INSERT INTO draft_sources VALUES(?,?)", [(did, mid) for mid in sources]
            )
        return {
            "id": did, "messages": values, "explanation": explanation,
            "status": "pending", "evidence": evidence, "source_message_ids": sources,
        }

    def draft(self, draft_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not row:
                raise Problem("Draft not found", 404)
            result = dict(row)
            result["messages"] = json.loads(result["messages"])
            result["evidence"] = json.loads(result["evidence"])
            result["source_message_ids"] = [
                r["message_id"] for r in db.execute(
                    "SELECT message_id FROM draft_sources WHERE draft_id=? ORDER BY rowid", (draft_id,)
                )
            ]
            return result

    def draft_action(self, draft_id, action, messages=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not row:
                raise Problem("Draft not found", 404)
            if row["status"] == "user_confirmed_sent" and action == "confirm_sent":
                if messages != json.loads(row["messages"]):
                    raise Problem("Confirmation retry differs from the saved messages", 409)
                return {"status": row["status"]}
            if row["status"] != "pending":
                raise Problem("Draft is no longer current", 409)
            a = self.account(db, row["account_id"])
            if a["person_id"] != row["person_id"] or a["revision"] != row["revision"]:
                raise Problem("Draft is stale", 409)
            source_ids = [
                r["message_id"] for r in db.execute(
                    "SELECT message_id FROM draft_sources WHERE draft_id=?", (draft_id,)
                )
            ]
            if action == "discard":
                db.execute(
                    "UPDATE drafts SET status='discarded',messages='[]',explanation='',evidence='[]' WHERE id=?",
                    (draft_id,),
                )
                db.execute("DELETE FROM draft_sources WHERE draft_id=?", (draft_id,))
                db.execute(
                    "UPDATE inbox SET status='dismissed',draft_id=NULL,updated_at=? WHERE draft_id=?",
                    (now(), draft_id),
                )
                return {"status": "discarded"}
            if action != "confirm_sent" or not isinstance(messages, list) or not 1 <= len(messages) <= 3:
                raise Problem("Explicit confirmation of 1-3 actually sent messages is required")
            values = [text(m, 1000) for m in messages]
            for index, content in enumerate(values):
                mid = uid()
                db.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        mid, a["id"], f"draft:{draft_id}:{index}", "self", "text",
                        content, None, now(), "user_confirmed_sent",
                    ),
                )
                db.executemany(
                    "INSERT INTO message_terms VALUES(?,?)", [(t, mid) for t in terms(content)]
                )
            db.execute(
                "UPDATE drafts SET status='user_confirmed_sent',messages=?,explanation='',evidence='[]' WHERE id=?",
                (json.dumps(values, ensure_ascii=False), draft_id),
            )
            db.execute("DELETE FROM draft_sources WHERE draft_id=?", (draft_id,))
            if source_ids:
                db.execute(
                    "UPDATE inbox SET status='handled',updated_at=? WHERE draft_id=?",
                    (now(), draft_id),
                )
            self.touch(db, [a["person_id"]])
            return {"status": "user_confirmed_sent"}

    def enqueue_inbox(self, account_id, message_ids):
        with self.db() as db:
            self.account(db, account_id)
            for mid in message_ids:
                row = db.execute(
                    "SELECT 1 FROM messages WHERE id=? AND account_id=? AND role='friend' AND kind='text'",
                    (mid, account_id),
                ).fetchone()
                if not row:
                    continue
                t = now()
                db.execute(
                    "INSERT OR IGNORE INTO inbox(id,account_id,message_id,status,created_at,updated_at) "
                    "VALUES(?,?,?,'new',?,?)",
                    (uid(), account_id, mid, t, t),
                )

    def pending_inbox(self, account_id=None, limit=100):
        with self.db() as db:
            sql = """SELECT i.*,m.content,m.occurred_at,m.observed_at
                     FROM inbox i JOIN messages m ON m.id=i.message_id
                     WHERE i.status='new'"""
            args = []
            if account_id:
                sql += " AND i.account_id=?"
                args.append(account_id)
            sql += " ORDER BY COALESCE(m.occurred_at,m.observed_at),m.rowid LIMIT ?"
            args.append(max(1, min(int(limit), 500)))
            return [dict(r) for r in db.execute(sql, args)]

    def attach_inbox_draft(self, message_ids, draft_id, status="drafted"):
        if status not in ("drafted", "queued"):
            raise Problem("Invalid inbox status")
        with self.db() as db:
            if not db.execute("SELECT 1 FROM drafts WHERE id=?", (draft_id,)).fetchone():
                raise Problem("Draft not found", 404)
            for mid in message_ids:
                db.execute(
                    "UPDATE inbox SET draft_id=?,status=?,updated_at=? WHERE message_id=?",
                    (draft_id, status, now(), mid),
                )

    def inbox_feed(self, limit=100):
        with self.db() as db:
            rows = db.execute(
                """SELECT i.id,i.account_id,i.message_id,i.status,i.draft_id,i.created_at,i.updated_at,
                          m.content,m.occurred_at,m.observed_at,a.label,p.name AS person_name,
                          d.messages AS draft_messages,d.explanation,d.status AS draft_status,
                          o.id AS outbox_id,o.state AS outbox_state,o.confirmed_count,o.last_error
                   FROM inbox i JOIN messages m ON m.id=i.message_id
                   JOIN accounts a ON a.id=i.account_id
                   JOIN persons p ON p.id=a.person_id
                   LEFT JOIN drafts d ON d.id=i.draft_id
                   LEFT JOIN outbox o ON o.draft_id=d.id
                   ORDER BY i.updated_at DESC LIMIT ?""",
                (max(1, min(int(limit), 300)),),
            ).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                item["draft_messages"] = json.loads(item["draft_messages"]) if item["draft_messages"] else []
                out.append(item)
            return out

    def skip_draft(self, draft_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not row:
                raise Problem("Draft not found", 404)
            if row["status"] != "pending":
                raise Problem("Draft is no longer current", 409)
            db.execute(
                "UPDATE drafts SET status='skipped',messages='[]',explanation='',evidence='[]' WHERE id=?",
                (draft_id,),
            )
            db.execute("DELETE FROM draft_sources WHERE draft_id=?", (draft_id,))
            db.execute(
                "UPDATE inbox SET status='handled',updated_at=? WHERE draft_id=?",
                (now(), draft_id),
            )
            return {"status": "skipped"}

    def queue_outbox(self, draft_id):
        with self.db() as db:
            d = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not d:
                raise Problem("Draft not found", 404)
            if d["status"] != "pending":
                raise Problem("Draft is no longer current", 409)
            a = self.account(db, d["account_id"])
            t = self.transport_row(db, a["id"], required=True)
            if t["mode"] != "B" or not t["enabled"]:
                raise Problem("B mode is not enabled for this account", 409)
            if a["person_id"] != d["person_id"] or a["revision"] != d["revision"]:
                raise Problem("Draft is stale", 409)
            oid = uid()
            stamp = now()
            db.execute(
                """INSERT INTO outbox(
                   id,draft_id,account_id,person_id,revision,payload,confirmed_count,state,
                   attempts,last_error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,0,'queued',0,'',?,?)""",
                (
                    oid, draft_id, a["id"], a["person_id"], a["revision"],
                    d["messages"], stamp, stamp,
                ),
            )
            db.execute("UPDATE drafts SET status='queued' WHERE id=?", (draft_id,))
            db.execute(
                "UPDATE inbox SET status='queued',updated_at=? WHERE draft_id=?",
                (stamp, draft_id),
            )
            return oid

    def claim_outbox(self):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE state='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            a = self.account(db, row["account_id"])
            t = self.transport_row(db, row["account_id"], required=True)
            if (
                a["person_id"] != row["person_id"] or a["revision"] != row["revision"]
                or t["mode"] != "B" or not t["enabled"]
            ):
                db.execute(
                    "UPDATE outbox SET state='cancelled',payload='[]',last_error='stale or disabled',updated_at=? WHERE id=?",
                    (now(), row["id"]),
                )
                db.execute(
                    "UPDATE drafts SET status='stale',messages='[]',explanation='',evidence='[]' WHERE id=?",
                    (row["draft_id"],),
                )
                db.execute("DELETE FROM draft_sources WHERE draft_id=?", (row["draft_id"],))
                return None
            db.execute(
                "UPDATE outbox SET state='sending',attempts=attempts+1,updated_at=? WHERE id=?",
                (now(), row["id"]),
            )
            result = dict(row)
            result["state"] = "sending"
            result["payload"] = json.loads(result["payload"])
            result["transport"] = t
            return result

    def confirm_outbox_chunk(self, outbox_id, index, content):
        content = text(content, 1000)
        with self.db() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            if not row or row["state"] != "sending":
                raise Problem("Outbox item is not sending", 409)
            payload = json.loads(row["payload"])
            if not (0 <= index < len(payload)) or payload[index] != content:
                raise Problem("Outbox payload changed", 409)
            if index < row["confirmed_count"]:
                return
            if index != row["confirmed_count"]:
                raise Problem("Outbox chunks must be confirmed in order", 409)
            existing = db.execute(
                "SELECT 1 FROM messages WHERE account_id=? AND source_key=?",
                (row["account_id"], f"outbox:{outbox_id}:{index}"),
            ).fetchone()
            if not existing:
                mid = uid()
                db.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        mid, row["account_id"], f"outbox:{outbox_id}:{index}",
                        "self", "text", content, None, now(), "wechat_verified_send",
                    ),
                )
                db.executemany(
                    "INSERT INTO message_terms VALUES(?,?)", [(t, mid) for t in terms(content)]
                )
            db.execute(
                "UPDATE outbox SET confirmed_count=confirmed_count+1,updated_at=? WHERE id=?",
                (now(), outbox_id),
            )

    def finish_outbox(self, outbox_id, state, error=""):
        if state not in ("confirmed", "unknown", "failed"):
            raise Problem("Invalid outbox result")
        with self.db() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            if not row:
                raise Problem("Outbox item not found", 404)
            if row["state"] not in ("sending", "failed"):
                raise Problem("Outbox item cannot be finished", 409)
            payload = json.loads(row["payload"])
            confirmed = int(row["confirmed_count"])
            if state == "confirmed" and confirmed != len(payload):
                raise Problem("Not every chunk was verified", 409)
            db.execute(
                "UPDATE outbox SET state=?,last_error=?,updated_at=? WHERE id=?",
                (state, text(error, 300) if error else "", now(), outbox_id),
            )
            if state == "confirmed":
                db.execute(
                    "UPDATE drafts SET status='wechat_verified_sent',explanation='',evidence='[]' WHERE id=?",
                    (row["draft_id"],),
                )
                db.execute("DELETE FROM draft_sources WHERE draft_id=?", (row["draft_id"],))
                db.execute(
                    "UPDATE inbox SET status='handled',updated_at=? WHERE draft_id=?",
                    (now(), row["draft_id"]),
                )
                self.touch(db, [row["person_id"]])
            elif state == "unknown":
                db.execute(
                    "UPDATE inbox SET status='send_unknown',updated_at=? WHERE draft_id=?",
                    (now(), row["draft_id"]),
                )
            else:
                db.execute(
                    "UPDATE inbox SET status='send_failed',updated_at=? WHERE draft_id=?",
                    (now(), row["draft_id"]),
                )

    def retry_outbox(self, outbox_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
            if not row or row["state"] != "failed":
                raise Problem("Only a confirmed-safe failure can be retried", 409)
            a = self.account(db, row["account_id"])
            t = self.transport_row(db, row["account_id"], required=True)
            if a["revision"] != row["revision"] or t["mode"] != "B" or not t["enabled"]:
                raise Problem("Context changed; generate a new reply instead", 409)
            db.execute(
                "UPDATE outbox SET state='queued',last_error='',updated_at=? WHERE id=?",
                (now(), outbox_id),
            )
            db.execute(
                "UPDATE inbox SET status='queued',updated_at=? WHERE draft_id=?",
                (now(), row["draft_id"]),
            )

    def delete_message(self, account_id, message_id):
        with self.db() as db:
            a = self.account(db, account_id)
            db.execute("DELETE FROM drafts WHERE person_id=?", (a["person_id"],))
            db.execute("DELETE FROM messages WHERE id=? AND account_id=?", (message_id, account_id))
            self.touch(db, [a["person_id"]])

    def export(self, account_id):
        with self.db() as db:
            self.account(db, account_id)
            return [
                dict(r) for r in db.execute(
                    "SELECT * FROM messages WHERE account_id=? "
                    "ORDER BY COALESCE(occurred_at,observed_at),rowid",
                    (account_id,),
                )
            ]
