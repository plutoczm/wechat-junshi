"""Account-scoped evidence; person-scoped retrieval. No WeChat database access."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid


class Problem(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def uid():
    return uuid.uuid4().hex


def terms(text):
    """Portable Chinese bigram / Latin word index, not semantic embeddings."""
    words = set(re.findall(r"[a-z0-9]{2,32}", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
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


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
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
            PRAGMA user_version = 1;
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass  # Windows permissions are managed by the owner; see deployment guide.

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
        row = db.execute("SELECT a.*,p.revision FROM accounts a JOIN persons p ON p.id=a.person_id WHERE a.id=?", (account_id,)).fetchone()
        if not row:
            raise Problem("Account not found", 404)
        return dict(row)

    @staticmethod
    def touch(db, person_ids):
        for person in set(person_ids):
            db.execute("UPDATE persons SET revision=revision+1 WHERE id=?", (person,))
            # Purge superseded context snapshots; do not retain cross-account evidence after a split.
            db.execute("UPDATE drafts SET status='stale',messages='[]',explanation='',evidence='[]' WHERE person_id=? AND status='pending'", (person,))

    def state(self):
        with self.db() as db:
            people = [dict(r) for r in db.execute("SELECT * FROM persons ORDER BY name,id")]
            for p in people:
                p["accounts"] = [dict(r) for r in db.execute("SELECT a.*, (SELECT COUNT(*) FROM messages m WHERE m.account_id=a.id) AS message_count FROM accounts a WHERE person_id=? ORDER BY label", (p["id"],))]
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
                raise Problem("Use a unique account label; labels are not verified WeChat IDs", 409) from exc
            self.touch(db, [person_id])
        return result

    def configure(self, account_id, cloud, mode="C"):
        if mode != "C":
            raise Problem("B mode unavailable: no verified free WeChat transport is connected", 409)
        if not isinstance(cloud, bool):
            raise Problem("cloud must be a boolean")
        with self.db() as db:
            a = self.account(db, account_id)
            db.execute("UPDATE accounts SET cloud=? WHERE id=?", (int(cloud), account_id))
            self.touch(db, [a["person_id"]])

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
            # Erase drafts for all accounts that could contain the deleted account's evidence.
            db.execute("DELETE FROM drafts WHERE person_id=?", (a["person_id"],))
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self.touch(db, [a["person_id"]])

    @staticmethod
    def normalize(rows, batch_id):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
            raise Problem("Provide 1-2000 messages per batch")
        batch_id = text(batch_id, 128)
        result = []
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) - {"id", "role", "kind", "content", "occurred_at"}:
                raise Problem("Invalid import fields; use the documented JSONL format")
            role, kind = row.get("role"), row.get("kind", "text")
            if role not in ("friend", "self", "system") or kind not in ("text", "sticker"):
                raise Problem("Invalid message role or kind")
            text(row.get("content"))  # validate without trimming the original imported text
            content = row["content"]
            timestamp = row.get("occurred_at")
            if timestamp is not None:
                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        raise ValueError()
                    timestamp = dt.astimezone(timezone.utc).isoformat()
                except (ValueError, TypeError, AttributeError) as exc:
                    raise Problem("occurred_at requires an ISO-8601 timezone, or null") from exc
            key = text(row["id"], 200) if row.get("id") is not None else "batch:" + batch_id + ":" + str(i)
            result.append((key, role, kind, content, timestamp))
        if len({r[0] for r in result}) != len(result):
            raise Problem("Duplicate source message IDs inside a batch")
        return result

    def ingest(self, account_id, rows, batch_id, confirm=False, expected_digest=None, origin="import"):
        normalized = self.normalize(rows, batch_id)
        digest = hashlib.sha256(json.dumps([account_id, normalized], ensure_ascii=False).encode()).hexdigest()
        if confirm and expected_digest != digest:
            raise Problem("Preview changed; preview again before importing", 409)
        ids, added = [], 0
        with self.db() as db:
            a = self.account(db, account_id)
            for key, role, kind, content, occurred in normalized:
                old = db.execute("SELECT * FROM messages WHERE account_id=? AND source_key=?", (account_id, key)).fetchone()
                if old:
                    if (old["role"], old["kind"], old["content"], old["occurred_at"]) != (role, kind, content, occurred):
                        raise Problem("Source ID conflict: existing message differs; nothing imported", 409)
                    ids.append(old["id"])
                    continue
                added += 1
                if confirm:
                    mid = uid()
                    db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", (mid, account_id, key, role, kind, content, occurred, now(), origin))
                    db.executemany("INSERT INTO message_terms VALUES(?,?)", [(t, mid) for t in terms(content)])
                    ids.append(mid)
            if confirm and added:
                self.touch(db, [a["person_id"]])
        return {"digest": digest, "total": len(rows), "new": added, "duplicates": len(rows) - added, "message_ids": ids}

    def add_note(self, account_id, content, message_id=None):
        content = text(content, 1000)
        with self.db() as db:
            a = self.account(db, account_id)
            if message_id and not db.execute("SELECT 1 FROM messages WHERE id=? AND account_id=?", (message_id, account_id)).fetchone():
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
            recent = [dict(r) for r in db.execute("SELECT m.*" + scope + " ORDER BY COALESCE(m.occurred_at,m.observed_at) DESC,m.rowid DESC LIMIT 20", args)]
            keywords = terms(query)[:40]
            matches = []
            if keywords:
                marks = ",".join("?" for _ in keywords)
                sql = "SELECT m.*,COUNT(*) AS score FROM message_terms t JOIN messages m ON m.id=t.message_id JOIN accounts a ON a.id=m.account_id WHERE a.person_id=?" + gate
                if cloud_only:
                    sql += " AND m.kind='text' AND m.role IN ('self','friend')"
                sql += f" AND t.term IN ({marks}) GROUP BY m.id ORDER BY score DESC,COALESCE(m.occurred_at,m.observed_at) DESC LIMIT 12"
                matches = [dict(r) for r in db.execute(sql, args + keywords)]
            notes = [dict(r) for r in db.execute("SELECT n.* FROM notes n JOIN accounts a ON a.id=n.account_id WHERE a.person_id=?" + gate + " ORDER BY n.created_at DESC LIMIT 30", args)]
            current = None
            if message_id:
                row = db.execute("SELECT * FROM messages WHERE id=? AND account_id=? AND role='friend' AND kind='text'", (message_id, account_id)).fetchone()
                if not row:
                    raise Problem("Choose a text message from this account", 404)
                current = dict(row)
                # Retrieval must use the actual selected message, not attacker-controlled IDs or aliases.
            combined = {r["id"]: r for r in matches + recent}
            records = sorted(combined.values(), key=lambda r: (r["occurred_at"] or r["observed_at"], r["id"]))
            return {"person_id": a["person_id"], "revision": a["revision"], "account_id": account_id,
                    "records": records, "notes": notes, "current": current}

    def save_draft(self, snapshot, messages, explanation):
        with self.db() as db:
            a = self.account(db, snapshot["account_id"])
            if not a["cloud"] or a["person_id"] != snapshot["person_id"] or a["revision"] != snapshot["revision"]:
                raise Problem("Context or consent changed while generating; generate again", 409)
            did = uid()
            evidence = [r["id"] for r in snapshot["records"]]
            db.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?)", (did, a["id"], a["person_id"], a["revision"], json.dumps(messages, ensure_ascii=False), explanation, json.dumps(evidence), "pending", now()))
        return {"id": did, "messages": messages, "explanation": explanation, "status": "pending", "evidence": evidence}

    def draft_action(self, draft_id, action, messages=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not row:
                raise Problem("Draft not found", 404)
            if row["status"] == "user_confirmed_sent" and action == "confirm_sent":
                if messages != json.loads(row["messages"]):
                    raise Problem("Confirmation retry differs from the saved messages", 409)
                return {"status": row["status"]}  # idempotent acknowledgement
            if row["status"] != "pending":
                raise Problem("Draft is no longer current", 409)
            a = self.account(db, row["account_id"])
            if a["person_id"] != row["person_id"] or a["revision"] != row["revision"]:
                raise Problem("Draft is stale", 409)
            if action == "discard":
                db.execute("UPDATE drafts SET status='discarded',messages='[]',explanation='',evidence='[]' WHERE id=?", (draft_id,))
                return {"status": "discarded"}
            if action != "confirm_sent" or not isinstance(messages, list) or not 1 <= len(messages) <= 3:
                raise Problem("Explicit confirmation of 1-3 actually sent messages is required")
            values = [text(m, 1000) for m in messages]
            # This is a USER REPORT, never a transport acknowledgement or an automatic send.
            for index, content in enumerate(values):
                mid = uid()
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", (mid, a["id"], f"draft:{draft_id}:{index}", "self", "text", content, None, now(), "user_confirmed_sent"))
                db.executemany("INSERT INTO message_terms VALUES(?,?)", [(t, mid) for t in terms(content)])
            db.execute("UPDATE drafts SET status='user_confirmed_sent',messages=?,explanation='',evidence='[]' WHERE id=?", (json.dumps(values, ensure_ascii=False), draft_id))
            self.touch(db, [a["person_id"]])
            return {"status": "user_confirmed_sent"}

    def delete_message(self, account_id, message_id):
        with self.db() as db:
            a = self.account(db, account_id)
            db.execute("DELETE FROM drafts WHERE person_id=?", (a["person_id"],))
            db.execute("DELETE FROM messages WHERE id=? AND account_id=?", (message_id, account_id))
            self.touch(db, [a["person_id"]])

    def export(self, account_id):
        with self.db() as db:
            self.account(db, account_id)
            return [dict(r) for r in db.execute("SELECT * FROM messages WHERE account_id=? ORDER BY COALESCE(occurred_at,observed_at),rowid", (account_id,))]
