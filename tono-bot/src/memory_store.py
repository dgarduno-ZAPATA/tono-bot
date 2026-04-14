import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import asyncpg

logger = logging.getLogger("BotTractos")

# ---- Connection config ----
# On Cloud Run: DB_HOST is a Unix socket path like /cloudsql/project:region:instance
# Locally / from Render: DB_HOST is a hostname or IP
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "tonobot")
DB_USER = os.getenv("DB_USER", "tonobot_app")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

_DEFAULT_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "45"))


class MemoryStore:
    """Session persistence backed by Cloud SQL (PostgreSQL) via asyncpg.

    Drop-in replacement for the previous Supabase-based store.
    Same public interface: init(), get(), upsert(), close(), purge_expired().

    TTL
    ---
    Uses a real ``expires_at`` column (TIMESTAMPTZ). Indexed.

    * ``get()``           filters ``expires_at > NOW()`` in the query itself.
    * ``upsert()``        refreshes ``expires_at = now + ttl_days`` on every write.
    * ``purge_expired()`` deletes rows where ``expires_at <= NOW()``.
    """

    def __init__(self, ttl_days: int = _DEFAULT_TTL_DAYS):
        self._ttl_days = ttl_days
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self):
        if not DB_HOST or not DB_PASSWORD:
            raise RuntimeError(
                "DB_HOST and DB_PASSWORD must be set. "
                "See deployment docs for Cloud SQL setup."
            )

        # asyncpg auto-detects Unix socket if host starts with /
        self._pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        # Verify connectivity
        async with self._pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        logger.info("✅ Cloud SQL MemoryStore connected.")

    async def get(self, phone: str) -> Optional[Dict[str, Any]]:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT phone, state, context_json
                    FROM sessions
                    WHERE phone = $1 AND expires_at > NOW()
                    LIMIT 1
                    """,
                    phone,
                )
        except Exception as e:
            logger.error(f"❌ SESSION GET failed | phone={phone} | error={e}")
            return None

        if not row:
            return None

        ctx = row["context_json"]
        # asyncpg returns JSONB as str; parse it
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception as e:
                logger.warning(f"⚠️ SESSION ctx parse error | phone={phone} | error={e}")
                ctx = {}
        elif ctx is None:
            ctx = {}

        return {
            "phone": row["phone"],
            "state": row["state"],
            "context_json": json.dumps(ctx, ensure_ascii=False),
            "context": ctx,
        }

    async def upsert(self, phone: str, state: str, context: Dict[str, Any]):
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=self._ttl_days)
        ctx_json = json.dumps(context, ensure_ascii=False)

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO sessions (phone, state, context_json, expires_at, updated_at)
                    VALUES ($1, $2, $3::jsonb, $4, $5)
                    ON CONFLICT (phone) DO UPDATE SET
                        state = EXCLUDED.state,
                        context_json = EXCLUDED.context_json,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    phone, state, ctx_json, expires_at, now,
                )
        except Exception as e:
            logger.error(f"❌ SESSION UPSERT failed | phone={phone} | state={state} | error={e}")

    async def purge_expired(self) -> int:
        """Delete sessions whose ``expires_at`` has passed. Returns row count."""
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM sessions WHERE expires_at <= NOW()"
                )
            # result is like "DELETE 42"
            deleted = int(result.split()[-1]) if result else 0
        except Exception as e:
            logger.error(f"⚠️ purge_expired falló: {e}")
            return -1

        if deleted:
            logger.info(f"🗑️ purge_expired: eliminadas {deleted} sesiones expiradas")
        return deleted

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
