import os
import httpx
import json
import logging

logger = logging.getLogger(__name__)

class MondayService:
    def __init__(self):
        # Llaves desde Render
        self.api_key = os.getenv("MONDAY_API_KEY")
        self.board_id = os.getenv("MONDAY_BOARD_ID")

        # 🔥 NUEVO: columna donde guardaremos el teléfono (dedupe)
        # Ejemplo: text6, phone, texto3, etc.
        self.phone_column_id = os.getenv("MONDAY_PHONE_COLUMN_ID")

        self.api_url = "https://api.monday.com/v2"

    # ============================================================
    # DEBUG: LISTAR COLUMNAS (para encontrar el column_id de Teléfono)
    # ============================================================
    async def debug_list_columns(self):
        """
        Imprime todas las columnas del board (id, title, type).
        Esto se ve en los Logs de Render.
        """
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

        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    self.api_url,
                    json={"query": query, "variables": {"boardId": int(self.board_id)}},
                    headers=headers
                )
                data = resp.json()

                if "errors" in data:
                    logger.error(f"❌ Error Monday Columns: {data['errors']}")
                    return

                cols = data["data"]["boards"][0]["columns"]
                logger.info("✅ Columnas detectadas en Monday:")
                for c in cols:
                    logger.info(f"➡️ ID={c['id']} | TITLE={c['title']} | TYPE={c['type']}")

            except Exception as e:
                logger.error(f"❌ Excepción listando columnas: {e}")

    # ============================================================
    # FIND EXISTING ITEM BY PHONE (DEDUPE)
    # ============================================================
    async def _find_item_by_phone(self, phone: str):
        """
        Busca un item por el valor de la columna Teléfono.
        Necesita:
        - phone_column_id en Render
        - phone no vacío
        """
        if not phone or not self.phone_column_id:
            return None

        query = """
        query ($boardId: ID!, $columnId: String!, $value: String!) {
          items_by_column_values (board_id: $boardId, column_id: $columnId, column_value: $value) {
            id
            name
          }
        }
        """

        variables = {
            "boardId": int(self.board_id),
            "columnId": self.phone_column_id,
            "value": phone
        }

        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    self.api_url,
                    json={"query": query, "variables": variables},
                    headers=headers
                )
                data = resp.json()

                if "errors" in data:
                    logger.error(f"❌ Error Monday Find: {data['errors']}")
                    return None

                items = data.get("data", {}).get("items_by_column_values", []) or []
                if not items:
                    return None

                return items[0]["id"]

            except Exception as e:
                logger.error(f"❌ Excepción buscando item por teléfono: {e}")
                return None

    # ============================================================
    # CREATE ITEM (ONLY ONCE)
    # ============================================================
    async def _create_item(self, item_name: str, phone: str):
        """
        Crea un item y guarda el teléfono en la columna.
        """
        query = """
        mutation ($boardId: ID!, $itemName: String!, $columnValues: JSON!) {
          create_item (board_id: $boardId, item_name: $itemName, column_values: $columnValues) {
            id
          }
        }
        """

        # Guardar Teléfono en columna (para dedupe futuro)
        column_values = {}
        if self.phone_column_id and phone:
            column_values[self.phone_column_id] = phone

        variables = {
            "boardId": int(self.board_id),
            "itemName": item_name,
            "columnValues": json.dumps(column_values)
        }

        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.api_url,
                json={"query": query, "variables": variables},
                headers=headers
            )
            data = resp.json()

            if "errors" in data:
                logger.error(f"❌ Error Monday Create: {data['errors']}")
                return None

            return data["data"]["create_item"]["id"]

    # ============================================================
    # CREATE UPDATE (ALWAYS)
    # ============================================================
    async def _create_update(self, item_id: str, body: str):
        query = """
        mutation ($itemId: ID!, $body: String!) {
          create_update (item_id: $itemId, body: $body) {
            id
          }
        }
        """

        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            await client.post(
                self.api_url,
                json={"query": query, "variables": {"itemId": int(item_id), "body": body}},
                headers=headers
            )

    # ============================================================
    # ✅ UPSERT LEAD (NO DUPLICADOS)
    # ============================================================
    async def create_lead(self, lead_data: dict):
        """
        (UPGRADE) create_lead ahora funciona como UPSERT:

        1) Si existe un item con ese teléfono -> NO crea uno nuevo, solo update.
        2) Si no existe -> crea item y luego update.

        ✅ Resultado: 1 solo lead por teléfono.
        """
        if not self.api_key or not self.board_id:
            logger.warning("⚠️ Faltan credenciales de Monday (API Key o Board ID).")
            return

        telefono = str(lead_data.get("telefono", "")).strip()
        nombre = str(lead_data.get("nombre", "Cliente Nuevo")).strip()

        if not self.phone_column_id:
            logger.warning("⚠️ MONDAY_PHONE_COLUMN_ID no está configurado en Render.")
            logger.warning("➡️ Se crearán duplicados porque Monday no puede deduplicar sin column_id.")
            # En este caso, seguimos con el flujo viejo (crea siempre)
            item_id = None
        else:
            # 1) Buscar si ya existe por teléfono
            item_id = await self._find_item_by_phone(telefono)

        # 2) Si no existe, crear
        if not item_id:
            item_name = nombre
            if telefono:
                item_name = f"{nombre} | {telefono}"

            item_id = await self._create_item(item_name=item_name, phone=telefono)

            if not item_id:
                logger.error("❌ No se pudo crear el item en Monday.")
                return

            logger.info(f"✅ Lead creado en Monday: {item_name} (ID: {item_id})")
        else:
            logger.info(f"♻️ Lead existente encontrado por teléfono: {telefono} (ID: {item_id})")

        # 3) Siempre guardar detalles como update (enriquecer)
        detalles = (
            f"📞 Teléfono: {lead_data.get('telefono', 'N/A')}\n"
            f"🚛 Interés: {lead_data.get('interes', 'N/A')}\n"
            f"📅 Cita Agendada: {lead_data.get('cita', 'Pendiente')}\n"
            f"💰 Forma de Pago: {lead_data.get('pago', 'N/A')}\n"
            f"🔥 Termómetro: {lead_data.get('termometro', 'N/A')}\n"
            f"📌 Acción: {lead_data.get('accion', 'N/A')}"
        )

        await self._create_update(item_id, detalles)
        logger.info("✅ Detalles guardados en Monday.")


# Instancia global
monday_service = MondayService()
