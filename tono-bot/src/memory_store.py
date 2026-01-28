import aiosqlite
from datetime import datetime
from typing import Optional, Dict, Any
import json
import os

DB_PATH = os.getenv("SQLITE_PATH", "/app/tono-bot/db/memory.db")

class MemoryStore:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            phone TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            context_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        await self._conn.commit()

    async def get(self, phone: str) -> Optional[Dict[str, Any]]:
        self._conn.row_factory = aiosqlite.Row
        cursor = await self._conn.execute(
            "SELECT phone, state, context_json FROM sessions WHERE phone=?", (phone,)
        )
        row = await cursor.fetchone()
        self._conn.row_factory = None
        if not row:
            return None
        data = dict(row)
        try:
            data["context"] = json.loads(data["context_json"] or "{}")
        except Exception:
            data["context"] = {}
        return data

    async def upsert(self, phone: str, state: str, context: Dict[str, Any]):
        now = datetime.utcnow().isoformat()
        ctx_json = json.dumps(context, ensure_ascii=False)
        await self._conn.execute("""
        INSERT INTO sessions(phone, state, context_json, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            state=excluded.state,
            context_json=excluded.context_json,
            updated_at=excluded.updated_at
        """, (phone, state, ctx_json, now))
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None
