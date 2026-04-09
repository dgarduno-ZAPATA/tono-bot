import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from supabase import create_client, Client

logger = logging.getLogger("BotTractos")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Sessions older than this are considered expired and will be ignored / deleted.
# Override via SESSION_TTL_DAYS env var.
_DEFAULT_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "90"))

# Internal key stored inside context_json — no DB migration required.
_EXPIRES_KEY = "_session_expires_at"


class MemoryStore:
    """Session persistence backed by Supabase (PostgreSQL).

    Drop-in replacement for the previous SQLite-based store.
    Same public interface: init(), get(), upsert(), close().

    TTL / purge
    -----------
    Each session stores its expiry timestamp inside context_json under the
    key ``_session_expires_at`` (ISO-8601, UTC).  No extra DB column is needed.

    * ``get()``            returns None for expired sessions (treats them as absent).
    * ``upsert()``         refreshes the expiry on every write.
    * ``purge_expired()``  deletes all rows whose stored expiry has passed.
                           Call it periodically (e.g. once at startup).
    """

    def __init__(self, url: str = SUPABASE_URL, key: str = SUPABASE_KEY,
                 ttl_days: int = _DEFAULT_TTL_DAYS):
        self._url = url
        self._key = key
        self._ttl_days = ttl_days
        self._client: Optional[Client] = None

    async def init(self):
        if not self._url or not self._key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set. "
                "See CLAUDE.md for setup instructions."
            )
        self._client = create_client(self._url, self._key)
        # Verify connectivity with a lightweight query
        self._client.table("sessions").select("phone").limit(1).execute()
        logger.info("✅ Supabase MemoryStore connected.")

    async def get(self, phone: str) -> Optional[Dict[str, Any]]:
        resp = (
            self._client
            .table("sessions")
            .select("phone, state, context_json")
            .eq("phone", phone)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return None
        row = resp.data[0]
        ctx = row.get("context_json") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}

        # --- TTL check ---
        exp_str = ctx.get(_EXPIRES_KEY)
        if exp_str:
            try:
                exp_dt = datetime.fromisoformat(exp_str)
                if datetime.now(timezone.utc) > exp_dt:
                    logger.info(f"🗑️ Sesión expirada para {phone} (exp={exp_str}) — tratando como nueva")
                    return None
            except Exception:
                pass  # malformed date → ignore, don't block the session

        return {
            "phone": row["phone"],
            "state": row["state"],
            "context_json": json.dumps(ctx, ensure_ascii=False) if isinstance(ctx, dict) else ctx,
            "context": ctx,
        }

    async def upsert(self, phone: str, state: str, context: Dict[str, Any]):
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(days=self._ttl_days)).isoformat()

        # Shallow-copy to avoid mutating the caller's dict
        ctx_with_ttl = {**context, _EXPIRES_KEY: expires_at}

        self._client.table("sessions").upsert(
            {
                "phone": phone,
                "state": state,
                "context_json": ctx_with_ttl,  # Supabase JSONB accepts dicts directly
                "updated_at": now.isoformat(),
            },
            on_conflict="phone",
        ).execute()

    async def purge_expired(self) -> int:
        """Delete sessions not updated within the TTL window.

        Filters by ``updated_at`` directly in Supabase — does NOT load
        context_json into memory, so it's safe to call on large tables.
        Returns the number of rows deleted, or -1 on error.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._ttl_days)).isoformat()
        try:
            resp = (
                self._client.table("sessions")
                .delete()
                .lt("updated_at", cutoff)
                .execute()
            )
            deleted = len(resp.data) if resp.data else 0
        except Exception as e:
            logger.error(f"⚠️ purge_expired falló: {e}")
            return -1

        if deleted:
            logger.info(f"🗑️ purge_expired: eliminadas {deleted} sesiones expiradas")
        return deleted

    async def close(self):
        # supabase-py uses httpx internally; no explicit close needed
        self._client = None
