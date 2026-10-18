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
        self.db.commit()

    def record(self, *, chat_id: int, message_id: int, sent_at: str,
               edited_at: str | None, text: str, reply_to: int | None,
               media_type: str | None, historical: bool) -> bool:
        payload = json.dumps([sent_at, edited_at, text, reply_to, media_type],
                             ensure_ascii=False).encode()
        revision = hashlib.sha256(payload).hexdigest()
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, revision, sent_at, edited_at, text, reply_to,
                 media_type, int(historical), datetime.now(timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

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
