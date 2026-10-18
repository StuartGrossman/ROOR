"""Private, append-only message revisions. This module cannot submit orders."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Archive:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                revision TEXT NOT NULL, sent_at TEXT NOT NULL, edited_at TEXT,
                text TEXT NOT NULL, reply_to INTEGER, media_type TEXT,
                historical INTEGER NOT NULL, captured_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id, revision)
            )
        """)
        self.db.execute("CREATE TABLE IF NOT EXISTS update_inbox (message_row INTEGER PRIMARY KEY, processed INTEGER NOT NULL DEFAULT 0)")
        self.db.commit()

    def record(self, *, chat_id: int, message_id: int, sent_at: str,
               edited_at: str | None, text: str, reply_to: int | None,
               media_type: str | None, historical: bool, enqueue: bool = False) -> bool:
        payload = json.dumps([sent_at, edited_at, text, reply_to, media_type],
                             ensure_ascii=False).encode()
        revision = hashlib.sha256(payload).hexdigest()
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, revision, sent_at, edited_at, text, reply_to,
                 media_type, int(historical), datetime.now(timezone.utc).isoformat()),
            )
            inserted=cursor.rowcount == 1
            if enqueue and not historical:
                self.db.execute("INSERT OR IGNORE INTO update_inbox (message_row) SELECT rowid FROM messages WHERE chat_id=? AND message_id=? AND revision=? AND historical=0", (chat_id,message_id,revision))
        return inserted

    def pending_updates(self,chat_id,limit=100):
        return [dict(r) for r in self.db.execute("SELECT m.rowid AS cursor,m.* FROM update_inbox i JOIN messages m ON m.rowid=i.message_row WHERE i.processed=0 AND m.chat_id=? ORDER BY m.rowid LIMIT ?",(chat_id,limit))]

    def ack_update(self,cursor):
        with self.db:self.db.execute("UPDATE update_inbox SET processed=1 WHERE message_row=?",(cursor,))

    def count(self, chat_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?",
                               (chat_id,)).fetchone()[0]

    def rows(self, chat_id: int) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY message_id, captured_at, rowid",
            (chat_id,),
        )]

    def close(self):
        self.db.close()
