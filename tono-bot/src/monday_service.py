import os
import json
import logging
import asyncio
import httpx
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class MondayService:
    def __init__(self):
        # Credenciales
        self.api_key = os.getenv("MONDAY_API_KEY")
        self.board_id = os.getenv("MONDAY_BOARD_ID")

        # Columnas (ya las tienes detectadas)
        # DEDUPE por mensaje (msg_id de Evolution): text_mkzvs0sw
        self.dedupe_column_id = os.getenv("MONDAY_DEDUPE_COLUMN_ID", "text_mkzvs0sw")

        # Teléfono: phone_mkzwh34a
        self.phone_column_id = os.getenv("MONDAY_PHONE_COLUMN_ID", "phone_mkzwh34a")

        # Estado: status (opcional, por si luego quieres setearlo)
        self.status_column_id = os.getenv("MONDAY_STATUS_COLUMN_ID", "status")

        self.api_url = "https://api.monday.com/v2"

    # ============================================================
    # Helpers
    # ============================================================
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self.api_key or "",
            "Content-Type": "application/json"
        }

    async def _post_monday(self, payload: Dict[str, Any], retries: int = 3) -> Dict[str, Any]:
        """
        POST robusto a Monday con reintentos sencillos.
        Maneja 429 / errores temporales sin tumbar el flujo.
        """
        if not self.api_key or not self.board_id:
            raise RuntimeError("Faltan credenciales de Monday: MONDAY_API_KEY o MONDAY_BOARD_ID")

        backoff = 0.6
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(1, retries + 1):
                try:
                    resp = await client.post(self.api_url, json=payload, headers=self._headers())

                    # Monday a veces responde 200 con "errors" en JSON. Igual lo parseamos.
                    data = resp.json()

                    # Rate limit / temporal
                    if resp.status_code == 429:
                        logger.warning(f"⏳ Monday 429 (rate limit). Reintento {attempt}/{retries} en {backoff:.1f}s")
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue

                    # Errores GraphQL (COMPLEXITY_BUDGET_EXHAUSTED, etc.)
                    if isinstance(data, dict) and data.get("errors"):
                        err_txt = str(data["errors"])
                        # Si es algo temporal, reintenta
                        if "COMPLEXITY" in err_txt or "rate" in err_txt.lower() or "timeout" in err_txt.lower():
                            logger.warning(f"⏳ Monday GraphQL error temporal. Reintento {attempt}/{retries} en {backoff:.1f}s | {err_txt}")
                            await asyncio.sleep(backoff)
                            backoff *= 2
                            continue
                        # Si no, lo devolvemos tal cual
                        return data

                    return data

                except Exception as e:
                    logger.warning(f"⚠️ Error POST Monday (attempt {attempt}/{retries}): {e}")
                    if attempt == retries:
                        raise
                    await asyncio.sleep(backoff)
                    backoff *= 2

        return {}

    # ============================================================
    # DEBUG: LISTAR COLUMNAS
    # ============================================================
    async def debug_list_columns(self):
        if not self.api_key or not self.board_id:
            logger.warning("⚠️ Faltan credenciales de Monday.")
            return

        query = """
        query ($boardId: ID!) {
          boards(ids: [$boardId]) {
            columns {
              id
              title
              type
            }
          }
        }
        """

        payload = {"query": query, "variables": {"boardId": int(self.board_id)}}
        data = await self._post_monday(payload)

        if data.get("errors"):
            logger.error(f"❌ Error Monday Columns: {data['errors']}")
            return

        cols = data["data"]["boards"][0]["columns"]
        logger.info("✅ Columnas detectadas en Monday:")
        for c in cols:
            logger.info(f"➡️ ID={c['id']} | TITLE={c['title']} | TYPE={c['type']}")

    # ============================================================
    # Buscar item por columna (items_page_by_column_values)
    # ============================================================
    async def _find_item_by_column_value(self, column_id: str, value: str) -> Optional[str]:
        if not value or not column_id:
            return None

        query = """
        query ($boardId: ID!, $columnId: String!, $val: String!) {
          items_page_by_column_values(
            board_id: $boardId,
            columns: [{column_id: $columnId, column_values: [$val]}],
            limit: 1
          ) {
            items {
              id
              name
            }
          }
        }
        """

        payload = {
            "query": query,
            "variables": {
                "boardId": int(self.board_id),
                "columnId": column_id,
                "val": value
            }
        }

        data = await self._post_monday(payload)

        if data.get("errors"):
            logger.error(f"❌ Error Monday Find ({column_id}={value}): {data['errors']}")
            return None

        items = (
            data.get("data", {})
                .get("items_page_by_column_values", {})
                .get("items", [])
        ) or []

        if not items:
            return None

        return str(items[0]["id"])

    # ============================================================
    # CREATE ITEM
    # ============================================================
    async def _create_item(self, item_name: str, telefono: str, external_id: Optional[str] = None) -> Optional[str]:
        query = """
        mutation ($boardId: ID!, $itemName: String!, $columnValues: JSON!) {
          create_item (board_id: $boardId, item_name: $itemName, column_values: $columnValues) {
            id
          }
        }
        """

        column_values: Dict[str, Any] = {}

        # Guardar external_id (msg_id) para dedupe fuerte
        if external_id:
            column_values[self.dedupe_column_id] = str(external_id)

        # Guardar teléfono (columna tipo phone requiere objeto)
        if telefono:
            column_values[self.phone_column_id] = {"phone": str(telefono), "countryShortName": "MX"}

        payload = {
            "query": query,
            "variables": {
                "boardId": int(self.board_id),
                "itemName": item_name,
                "columnValues": json.dumps(column_values)
            }
        }

        data = await self._post_monday(payload)

        if data.get("errors"):
            logger.error(f"❌ Error Monday Create: {data['errors']}")
            return None

        return str(data["data"]["create_item"]["id"])

    # ============================================================
    # CREATE UPDATE (comentario)
    # ============================================================
    async def _create_update(self, item_id: str, body: str):
        query = """
        mutation ($itemId: ID!, $body: String!) {
          create_update (item_id: $itemId, body: $body) {
            id
          }
        }
        """

        payload = {
            "query": query,
            "variables": {"itemId": int(item_id), "body": body}
        }

        data = await self._post_monday(payload)
        if data.get("errors"):
            logger.error(f"❌ Error Monday Create Update: {data['errors']}")

    # ============================================================
    # ✅ UPSERT LEAD (NO DUPLICADOS)
    # ============================================================
    async def create_lead(self, lead_data: dict):
        """
        UPSERT inteligente (anti-duplicados real):

        1) Si viene external_id (msg_id) -> busca por ese ID en columna text_mkzvs0sw
           - Si existe: NO crea item, solo agrega update.
           - Si no: crea item guardando external_id + teléfono.
        2) Si NO viene external_id -> fallback por teléfono (menos perfecto).

        ✅ Resultado: aunque Evolution reintente 10 veces, Monday solo tendrá 1 item.
        """
        if not self.api_key or not self.board_id:
            logger.warning("⚠️ Faltan credenciales de Monday (API Key o Board ID).")
            return

        telefono = str(lead_data.get("telefono", "")).strip()
        nombre = str(lead_data.get("nombre", "Cliente Nuevo")).strip()

        # ESTE ES TU CANDADO DE HIERRO
        external_id = (
            lead_data.get("external_id")
            or lead_data.get("message_id")
            or lead_data.get("msg_id")
        )
        external_id = str(external_id).strip() if external_id else None

        item_id: Optional[str] = None

        # 1) DEDUPE FUERTE por external_id
        if external_id:
            item_id = await self._find_item_by_column_value(self.dedupe_column_id, external_id)
            if item_id:
                logger.info(f"🧱 Lead ya existe por external_id={external_id} (ID: {item_id})")

        # 2) Fallback por teléfono (si no hay external_id o no encontró)
        if not item_id and telefono:
            # Nota: buscar por phone a veces depende de cómo Monday indexa teléfonos;
            # por eso external_id es el método recomendado.
            item_id = await self._find_item_by_column_value(self.phone_column_id, telefono)
            if item_id:
                logger.info(f"♻️ Lead existente encontrado por teléfono={telefono} (ID: {item_id})")

        # 3) Si no existe, crear item nuevo
        if not item_id:
            item_name = nombre
            if telefono:
                item_name = f"{nombre} | {telefono}"

            item_id = await self._create_item(item_name=item_name, telefono=telefono, external_id=external_id)

            if not item_id:
                logger.error("❌ No se pudo crear el item en Monday.")
                return

            logger.info(f"✅ Lead creado en Monday: {item_name} (ID: {item_id}) | external_id={external_id}")

        # 4) Siempre agregar update con detalles
        detalles = (
            f"📩 external_id: {external_id or 'N/A'}\n"
            f"📞 Teléfono: {lead_data.get('telefono', 'N/A')}\n"
            f"👤 Nombre: {lead_data.get('nombre', 'N/A')}\n"
            f"🚛 Interés: {lead_data.get('interes', 'N/A')}\n"
            f"📅 Cita Agendada: {lead_data.get('cita', 'Pendiente')}\n"
            f"💰 Forma de Pago: {lead_data.get('pago', 'N/A')}\n"
            f"🔥 Termómetro: {lead_data.get('termometro', 'N/A')}\n"
            f"📌 Acción: {lead_data.get('accion', 'N/A')}"
        )

        await self._create_update(item_id, detalles)
        logger.info("✅ Detalles guardados en Monday (update).")


# Instancia global
monday_service = MondayService()
