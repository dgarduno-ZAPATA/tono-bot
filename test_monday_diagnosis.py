#!/usr/bin/env python3
"""
Monday.com Integration Diagnostic Script
Corre desde Cloud Shell para diagnosticar problemas de registro de leads.

Uso:
  export MONDAY_API_KEY="..."
  export MONDAY_BOARD_ID="18396811838"
  export MONDAY_DEDUPE_COLUMN_ID="text_mkzw7xjz"
  export MONDAY_STAGE_COLUMN_ID="status"
  export MONDAY_VEHICLE_COLUMN_ID="dropdown_mm0gq48r"
  export MONDAY_PAYMENT_COLUMN_ID="color_mm0gbjea"
  # ... (el resto de columnas)
  python3 test_monday_diagnosis.py
"""

import os
import json
import asyncio
import httpx
import sys

# ============================================================
# CONFIG: Column IDs esperados (del CLAUDE.md / env vars)
# ============================================================
EXPECTED_COLS = {
    "MONDAY_API_KEY":                 ("API Key",             True),
    "MONDAY_BOARD_ID":                ("Board ID",            True),
    "MONDAY_DEDUPE_COLUMN_ID":        ("Phone Dedupe",        True),
    "MONDAY_STAGE_COLUMN_ID":         ("Etapa (status)",      True),
    "MONDAY_VEHICLE_COLUMN_ID":       ("Vehículo (dropdown)", True),
    "MONDAY_PAYMENT_COLUMN_ID":       ("Pago (status)",       True),
    "MONDAY_PHONE_COLUMN_ID":         ("Teléfono real",       False),
    "MONDAY_LAST_MSG_ID_COLUMN_ID":   ("Last Msg ID",         False),
    "MONDAY_APPOINTMENT_COLUMN_ID":   ("Cita (fecha)",        False),
    "MONDAY_APPOINTMENT_TIME_COLUMN_ID": ("Cita (hora)",      False),
    "MONDAY_SOURCE_COLUMN_ID":        ("Origen Lead",         False),
    "MONDAY_CHANNEL_COLUMN_ID":       ("Canal",               False),
    "MONDAY_SOURCE_TYPE_COLUMN_ID":   ("Tipo Origen",         False),
}

SEP = "=" * 70

def hr(title=""):
    if title:
        pad = (70 - len(title) - 2) // 2
        print(f"\n{'─'*pad} {title} {'─'*pad}")
    else:
        print("─" * 70)

# ============================================================
# STEP 1: Verificar env vars
# ============================================================
def check_env_vars():
    hr("STEP 1: ENV VARS")
    missing_critical = []
    for var, (label, critical) in EXPECTED_COLS.items():
        val = os.getenv(var, "")
        if val:
            display = val[:6] + "..." if var == "MONDAY_API_KEY" else val
            print(f"  ✅  {var:<40} = {display}  ({label})")
        else:
            marker = "❌ CRÍTICA" if critical else "⚠️  opcional"
            print(f"  {marker}  {var:<40}  ({label})")
            if critical:
                missing_critical.append(var)

    if missing_critical:
        print(f"\n  🚨 Faltan variables CRÍTICAS: {missing_critical}")
        print("     Sin estas variables las columnas de Monday quedarán vacías.")
    else:
        print("\n  ✅ Todas las variables críticas están configuradas.")
    return missing_critical


# ============================================================
# STEP 2: Test conexión Monday API
# ============================================================
async def test_api_connection():
    hr("STEP 2: CONEXIÓN A MONDAY API")
    api_key = os.getenv("MONDAY_API_KEY", "")
    board_id = os.getenv("MONDAY_BOARD_ID", "")

    if not api_key or not board_id:
        print("  ⛔ Saltando — faltan MONDAY_API_KEY o MONDAY_BOARD_ID")
        return False

    query = "query { me { name email } }"
    headers = {"Authorization": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.monday.com/v2",
                json={"query": query},
                headers=headers
            )
        data = resp.json()
        if "errors" in data:
            print(f"  ❌ Error de API: {data['errors']}")
            return False
        me = data.get("data", {}).get("me", {})
        print(f"  ✅ Conectado como: {me.get('name')} <{me.get('email')}>")
        return True
    except Exception as e:
        print(f"  ❌ Excepción: {e}")
        return False


# ============================================================
# STEP 3: Verificar columnas del board
# ============================================================
async def check_board_columns():
    hr("STEP 3: COLUMNAS DEL BOARD EN MONDAY")
    api_key = os.getenv("MONDAY_API_KEY", "")
    board_id = os.getenv("MONDAY_BOARD_ID", "")

    if not api_key or not board_id:
        print("  ⛔ Saltando")
        return {}

    query = """
    query ($board_id: ID!) {
      boards(ids: [$board_id]) {
        name
        columns {
          id
          title
          type
        }
      }
    }
    """
    headers = {"Authorization": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.monday.com/v2",
                json={"query": query, "variables": {"board_id": int(board_id)}},
                headers=headers
            )
        data = resp.json()
        boards = data.get("data", {}).get("boards", [])
        if not boards:
            print(f"  ❌ Board {board_id} no encontrado o sin acceso")
            return {}

        board = boards[0]
        print(f"  Board: '{board.get('name')}' (ID: {board_id})")
        columns = {c["id"]: c for c in board.get("columns", [])}

        # Map configured IDs against real columns
        col_vars = {
            "MONDAY_DEDUPE_COLUMN_ID":           "Phone Dedupe",
            "MONDAY_STAGE_COLUMN_ID":            "Etapa",
            "MONDAY_VEHICLE_COLUMN_ID":          "Vehículo",
            "MONDAY_PAYMENT_COLUMN_ID":          "Pago",
            "MONDAY_PHONE_COLUMN_ID":            "Teléfono real",
            "MONDAY_APPOINTMENT_COLUMN_ID":      "Cita fecha",
            "MONDAY_APPOINTMENT_TIME_COLUMN_ID": "Cita hora",
            "MONDAY_SOURCE_COLUMN_ID":           "Origen",
            "MONDAY_CHANNEL_COLUMN_ID":          "Canal",
            "MONDAY_SOURCE_TYPE_COLUMN_ID":      "Tipo Origen",
        }

        print("\n  Verificando IDs configurados vs columnas reales del board:")
        for var, label in col_vars.items():
            col_id = os.getenv(var, "")
            if not col_id:
                print(f"    ⚪  {label:<20} — {var} no configurada")
                continue
            if col_id in columns:
                col_info = columns[col_id]
                print(f"    ✅  {label:<20} — ID '{col_id}' → '{col_info['title']}' (type: {col_info['type']})")
            else:
                print(f"    ❌  {label:<20} — ID '{col_id}' NO EXISTE en el board")
                print(f"         Columnas disponibles con tipo parecido:")
                for cid, cinfo in columns.items():
                    print(f"           {cid:<30} '{cinfo['title']}'  ({cinfo['type']})")

        return columns
    except Exception as e:
        print(f"  ❌ Excepción: {e}")
        return {}


# ============================================================
# STEP 4: Buscar lead existente por teléfono (dedup test)
# ============================================================
async def test_dedup_lookup(test_phone: str):
    hr("STEP 4: TEST BÚSQUEDA POR TELÉFONO (dedup)")
    api_key = os.getenv("MONDAY_API_KEY", "")
    board_id = os.getenv("MONDAY_BOARD_ID", "")
    dedupe_col = os.getenv("MONDAY_DEDUPE_COLUMN_ID", "")

    if not api_key or not board_id or not dedupe_col:
        print("  ⛔ Saltando — faltan vars")
        return None

    print(f"  Buscando teléfono: {test_phone}  en columna: {dedupe_col}")
    query = """
    query ($board_id: ID!, $col_id: String!, $val: String!) {
      items_page_by_column_values(limit: 5, board_id: $board_id,
        columns: [{column_id: $col_id, column_values: [$val]}]) {
        items { id name column_values { id text value } }
      }
    }
    """
    headers = {"Authorization": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.monday.com/v2",
                json={"query": query, "variables": {
                    "board_id": int(board_id),
                    "col_id": dedupe_col,
                    "val": test_phone,
                }},
                headers=headers
            )
        data = resp.json()
        if "errors" in data:
            print(f"  ❌ Errores en búsqueda: {data['errors']}")
            return None

        items = data.get("data", {}).get("items_page_by_column_values", {}).get("items", [])
        if items:
            item = max(items, key=lambda x: int(x.get("id", 0)))
            stage_col = os.getenv("MONDAY_STAGE_COLUMN_ID", "status")
            current_stage = ""
            for col in item.get("column_values", []):
                if col.get("id") == stage_col:
                    current_stage = col.get("text", "")
            print(f"  ✅ Lead encontrado: ID={item['id']}  nombre='{item['name']}'  etapa='{current_stage}'")
            return item["id"]
        else:
            print(f"  ℹ️  No hay lead existente con ese teléfono — se creará uno nuevo")
            return None
    except Exception as e:
        print(f"  ❌ Excepción: {e}")
        return None


# ============================================================
# STEP 5: Crear lead de prueba con todas las columnas
# ============================================================
async def test_create_lead(test_phone: str):
    hr("STEP 5: CREAR LEAD DE PRUEBA CON TODAS LAS COLUMNAS")
    api_key = os.getenv("MONDAY_API_KEY", "")
    board_id = os.getenv("MONDAY_BOARD_ID", "")

    if not api_key or not board_id:
        print("  ⛔ Saltando")
        return None

    # Build column values from configured env vars
    col_vals = {}

    dedupe_col = os.getenv("MONDAY_DEDUPE_COLUMN_ID", "")
    if dedupe_col:
        col_vals[dedupe_col] = test_phone
        print(f"  + Dedupe phone: {dedupe_col} = {test_phone}")

    stage_col = os.getenv("MONDAY_STAGE_COLUMN_ID", "")
    if stage_col:
        col_vals[stage_col] = {"label": "1er Contacto"}
        print(f"  + Stage: {stage_col} = '1er Contacto'")

    vehicle_col = os.getenv("MONDAY_VEHICLE_COLUMN_ID", "")
    if vehicle_col:
        col_vals[vehicle_col] = {"labels": ["Tunland G9"]}
        print(f"  + Vehicle: {vehicle_col} = 'Tunland G9'")

    payment_col = os.getenv("MONDAY_PAYMENT_COLUMN_ID", "")
    if payment_col:
        col_vals[payment_col] = {"label": "Por definir"}
        print(f"  + Payment: {payment_col} = 'Por definir'")

    phone_col = os.getenv("MONDAY_PHONE_COLUMN_ID", "")
    if phone_col:
        col_vals[phone_col] = {"phone": test_phone, "countryShortName": "MX"}
        print(f"  + Phone col: {phone_col} = {test_phone}")

    source_col = os.getenv("MONDAY_SOURCE_COLUMN_ID", "")
    if source_col:
        col_vals[source_col] = {"label": "Directo"}
        print(f"  + Source: {source_col} = 'Directo'")

    channel_col = os.getenv("MONDAY_CHANNEL_COLUMN_ID", "")
    if channel_col:
        col_vals[channel_col] = {"label": "Directo"}
        print(f"  + Channel: {channel_col} = 'Directo'")

    source_type_col = os.getenv("MONDAY_SOURCE_TYPE_COLUMN_ID", "")
    if source_type_col:
        col_vals[source_type_col] = {"label": "Directo"}
        print(f"  + Source type: {source_type_col} = 'Directo'")

    print(f"\n  Total columnas a enviar: {len(col_vals)}")
    col_vals_json = json.dumps(col_vals)
    print(f"\n  JSON de columnas:\n  {col_vals_json}\n")

    # Create item
    item_name = f"TEST DIAGNOSTICO | {test_phone}"
    query = """
    mutation ($board_id: ID!, $name: String!, $vals: JSON!) {
        create_item (
            board_id: $board_id,
            item_name: $name,
            column_values: $vals,
            create_labels_if_missing: true
        ) { id }
    }
    """
    headers = {"Authorization": api_key, "Content-Type": "application/json"}

    print(f"  Creando item '{item_name}'...")
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                "https://api.monday.com/v2",
                json={"query": query, "variables": {
                    "board_id": int(board_id),
                    "name": item_name,
                    "vals": col_vals_json,
                }},
                headers=headers
            )

        print(f"  HTTP Status: {resp.status_code}")
        data = resp.json()

        if "errors" in data:
            print(f"\n  ❌ ERRORES DE MONDAY API:")
            for err in data["errors"]:
                print(f"     - {err.get('message', err)}")

        if data.get("data", {}).get("create_item", {}).get("id"):
            item_id = data["data"]["create_item"]["id"]
            print(f"\n  ✅ Item creado: ID = {item_id}")
            print(f"     Verifica en Monday que TODAS las columnas estén llenas.")
            return item_id
        else:
            print(f"\n  ❌ No se obtuvo ID. Respuesta completa:")
            print(f"     {json.dumps(data, indent=2, ensure_ascii=False)}")
            return None

    except Exception as e:
        print(f"  ❌ Excepción: {e}")
        return None


# ============================================================
# STEP 6: Limpiar item de prueba
# ============================================================
async def delete_test_item(item_id: str):
    hr("STEP 6: ELIMINAR ITEM DE PRUEBA")
    api_key = os.getenv("MONDAY_API_KEY", "")
    if not api_key or not item_id:
        return

    query = "mutation ($id: ID!) { delete_item (item_id: $id) { id } }"
    headers = {"Authorization": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.monday.com/v2",
                json={"query": query, "variables": {"id": int(item_id)}},
                headers=headers
            )
        data = resp.json()
        if data.get("data", {}).get("delete_item"):
            print(f"  ✅ Item {item_id} eliminado.")
        else:
            print(f"  ⚠️  No se pudo eliminar item {item_id}: {data}")
    except Exception as e:
        print(f"  ⚠️  Error eliminando: {e}")


# ============================================================
# MAIN
# ============================================================
async def main():
    print(SEP)
    print("  MONDAY.COM INTEGRATION DIAGNOSTIC")
    print("  Tono-Bot — Tractos y Max")
    print(SEP)

    # Test phone (won't collide with real leads)
    TEST_PHONE = os.getenv("TEST_PHONE", "5500000000")

    # Step 1
    missing = check_env_vars()

    # Step 2
    ok = await test_api_connection()
    if not ok:
        print("\n  🚨 No se pudo conectar a Monday API. Revisar MONDAY_API_KEY.")
        sys.exit(1)

    # Step 3
    await check_board_columns()

    # Step 4
    await test_dedup_lookup(TEST_PHONE)

    # Step 5
    item_id = await test_create_lead(TEST_PHONE)

    # Step 6 (cleanup)
    if item_id:
        hr("¿Eliminar el item de prueba?")
        try:
            answer = input("  ¿Eliminar item de prueba de Monday? (s/N): ").strip().lower()
        except EOFError:
            answer = "s"
        if answer in ("s", "si", "sí", "y", "yes"):
            await delete_test_item(item_id)
        else:
            print(f"  Item {item_id} conservado en Monday para inspección manual.")

    hr("FIN DEL DIAGNÓSTICO")
    if missing:
        print(f"\n  🚨 ACCIÓN REQUERIDA: Configurar estas env vars en Cloud Run:")
        for var in missing:
            label = EXPECTED_COLS[var][0]
            print(f"     {var}  ({label})")
    else:
        print("\n  ✅ Diagnóstico completo. Revisa los resultados arriba.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
