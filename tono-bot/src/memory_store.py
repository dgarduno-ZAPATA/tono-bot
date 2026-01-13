import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any
import json
import os

DB_PATH = os.getenv("SQLITE_PATH", "/app/tono-bot/db/memory.db")

class MemoryStore:
    def __init__(self, path: str = DB_PATH):
        self.path = path

    def init(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                phone TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                context_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
            conn.commit()

    def get(self, phone: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT phone, state, context_json FROM sessions WHERE phone=?", (phone,))
            row = c.fetchone()
            if not row:
                return None
            data = dict(row)
            try:
                data["context"] = json.loads(data["context_json"] or "{}")
            except Exception:
                data["context"] = {}
            return data

    def upsert(self, phone: str, state: str, context: Dict[str, Any]):
        now = datetime.utcnow().isoformat()
        ctx_json = json.dumps(context, ensure_ascii=False)
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("""
            INSERT INTO sessions(phone, state, context_json, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                state=excluded.state,
                context_json=excluded.context_json,
                updated_at=excluded.updated_at
            """, (phone, state, ctx_json, now))
            conn.commit()
