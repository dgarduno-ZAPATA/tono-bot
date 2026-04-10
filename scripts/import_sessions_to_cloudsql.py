"""
Import sessions from Supabase JSON export to Cloud SQL PostgreSQL.

Usage (Windows PowerShell):
    $env:DB_HOST="34.51.17.96"
    $env:DB_PASSWORD="your_password_here"
    python scripts/import_sessions_to_cloudsql.py

Optional env vars:
    DB_PORT (default 5432)
    DB_NAME (default tonobot)
    DB_USER (default tonobot_app)

Optional argument:
    python scripts/import_sessions_to_cloudsql.py path/to/export.json
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import asyncpg


# ---- Config from env ----
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "tonobot")
DB_USER = os.getenv("DB_USER", "tonobot_app")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# ---- Paths ----
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSON_PATH = REPO_ROOT / "sessions_export.json"

EXPECTED_KEYS = {"phone", "state", "context_json", "updated_at", "expires_at"}
BATCH_SIZE = 50

UPSERT_SQL = """
INSERT INTO sessions (phone, state, context_json, expires_at, updated_at)
VALUES ($1, $2, $3::jsonb, $4, $5)
ON CONFLICT (phone) DO UPDATE SET
    state = EXCLUDED.state,
    context_json = EXCLUDED.context_json,
    expires_at = EXCLUDED.expires_at,
    updated_at = EXCLUDED.updated_at
"""


def validate_env():
    missing = []
    if not DB_HOST:
        missing.append("DB_HOST")
    if not DB_PASSWORD:
        missing.append("DB_PASSWORD")
    if missing:
        print(f"❌ Missing required env vars: {', '.join(missing)}")
        print("\nSet them in PowerShell with:")
        print('  $env:DB_HOST="34.51.17.96"')
        print('  $env:DB_PASSWORD="your_password"')
        sys.exit(1)


def load_json(path: Path):
    if not path.exists():
        print(f"❌ File not found: {path}")
        sys.exit(1)

    print(f"📂 Loading {path}...")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        print(f"❌ Expected a JSON array, got {type(data).__name__}")
        sys.exit(1)

    if not data:
        print("❌ JSON is empty")
        sys.exit(1)

    first_keys = set(data[0].keys())
    missing_keys = EXPECTED_KEYS - first_keys
    if missing_keys:
        print(f"❌ First row missing keys: {missing_keys}")
        print(f"   Found keys: {first_keys}")
        sys.exit(1)

    print(f"✅ Loaded {len(data)} rows")
    return data


def parse_row(row: dict):
    """Convert a JSON row to params for the upsert query."""
    phone = row["phone"]
    state = row["state"] or "INITIAL"

    ctx = row["context_json"]
    if ctx is None:
        ctx = {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except Exception:
            ctx = {}
    ctx_json = json.dumps(ctx, ensure_ascii=False)

    updated_at = datetime.fromisoformat(row["updated_at"])
    expires_at = datetime.fromisoformat(row["expires_at"])

    return (phone, state, ctx_json, expires_at, updated_at)


async def import_data(rows):
    print(f"\n🔌 Connecting to {DB_HOST}:{DB_PORT}/{DB_NAME} as {DB_USER}...")
    pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=1,
        max_size=5,
        ssl="require",
        command_timeout=30,
    )
    print("✅ Connected.")

    total = len(rows)
    success = 0
    errors = []

    try:
        for i in range(0, total, BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for row in batch:
                        try:
                            params = parse_row(row)
                            await conn.execute(UPSERT_SQL, *params)
                            success += 1
                        except Exception as e:
                            phone = row.get("phone", "?")
                            errors.append((phone, str(e)))
                            print(f"  ⚠️  Error en {phone}: {e}")
            done = min(i + BATCH_SIZE, total)
            print(f"  Procesadas {done}/{total}...")

        async with pool.acquire() as conn:
            db_count = await conn.fetchval("SELECT COUNT(*) FROM sessions")

        print(f"\n✅ Import completo: {success} insertadas/actualizadas, {len(errors)} errores")
        print(f"📊 Total filas en sessions table: {db_count}")
        if db_count == total:
            print(f"✅ Count verificado: {db_count} == {total}")
        else:
            print(f"⚠️  Count no coincide: {db_count} != {total}")

        if errors:
            print(f"\n⚠️  {len(errors)} errores:")
            for phone, msg in errors[:10]:
                print(f"  - {phone}: {msg}")
            if len(errors) > 10:
                print(f"  ... y {len(errors) - 10} más")

    finally:
        await pool.close()
        print("🔌 Pool cerrado.")


def main():
    validate_env()

    json_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_JSON_PATH
    rows = load_json(json_path)

    print(f"\n⚠️  Vas a importar {len(rows)} filas a:")
    print(f"   Host: {DB_HOST}")
    print(f"   DB:   {DB_NAME}")
    print(f"   User: {DB_USER}")
    confirm = input("\n¿Continuar? Escribe 'yes' para confirmar: ").strip()
    if confirm != "yes":
        print("Cancelado.")
        sys.exit(0)

    asyncio.run(import_data(rows))


if __name__ == "__main__":
    main()
