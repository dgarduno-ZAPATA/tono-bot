import os
import httpx
import json
import logging

logger = logging.getLogger(__name__)

class MondayService:
    def __init__(self):
        self.api_key = os.getenv("MONDAY_API_KEY")
        self.board_id = os.getenv("MONDAY_BOARD_ID")
        self.api_url = "https://api.monday.com/v2"

        # Dedupe por teléfono (columna TEXTO "Telefono Dedupe")
        self.phone_dedupe_text_column_id = os.getenv("MONDAY_PHONE_COLUMN_ID")  # <- ya lo pusimos así en Render

        # Guardar el último msg_id (columna TEXTO "Last Msg ID")
        self.last_msg_id_column_id = os.getenv("MONDAY_LAST_MSG_ID_COLUMN_ID")

        # Columna phone real (opcional). Si quieres llenar "Teléfono" tipo phone:
        # En tu board es phone_mkzwh34a, pero si quieres también lo dejamos configurable:
        self.phone_column_real_id = os.getenv("MONDAY_PHONE_REAL_COLUMN_ID")  # opcional

    async def _graphql(self, query: str, variables: dict):
        if not self.api_key:
            raise RuntimeError("MONDAY_API_KEY no configurada")

        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(self.api_url, json={"query": query, "variables": variables}, headers=headers)

        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"Monday GraphQL errors: {data['errors']}")
        return data

    async def _find_item_by_phone_dedupe(self, phone: str):
        """
        Busca item por columna TEXTO "Telefono Dedupe".
        Usamos items_page_by_column_values (correcto, el viejo items_by_column_values ya no conviene).
        """
        if not phone or not self.phone_dedupe_text_column_id:
            return None

        query = """
        query ($board_id: ID!, $col_id: String!, $val: String!) {
          items_page_by_column_values(
            limit: 1,
            board_id: $board_id,
            columns: [{column_id: $col_id, column_values: [$val]}]
          ) {
            items { id name }
          }
        }
        """
        variables = {"board_id": int(self.board_id), "col_id": self.phone_dedupe_text_column_id, "val": phone}
        data = await self._graphql(query, variables)

        items = data.get("data", {}).get("items_page_by_column_values", {}).get("items", []) or []
        if not items:
            return None
        return items[0]["id"]

    async def _create_item(self, item_name: str, phone: str, msg_id: str = ""):
        query = """
        mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
          create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) { id }
        }
        """

        col_vals = {}

        # ✅ Telefono Dedupe (texto)
        if self.phone_dedupe_text_column_id and phone:
            col_vals[self.phone_dedupe_text_column_id] = phone

        # ✅ Last Msg ID (texto)
        if self.last_msg_id_column_id and msg_id:
            col_vals[self.last_msg_id_column_id] = msg_id

        # ✅ Teléfono real tipo phone (opcional)
        if self.phone_column_real_id and phone:
            # monday phone column espera objeto {phone, countryShortName}
            col_vals[self.phone_column_real_id] = {"phone": phone, "countryShortName": "MX"}

        variables = {
            "board_id": int(self.board_id),
            "item_name": item_name,
            "column_values": json.dumps(col_vals),
        }

        data = await self._graphql(query, variables)
        return data["data"]["create_item"]["id"]

    async def _update_columns(self, item_id: str, col_vals: dict):
        """
        Actualiza columnas (por ejemplo Last Msg ID) en item existente.
        """
        if not col_vals:
            return

        query = """
        mutation ($item_id: ID!, $board_id: ID!, $column_values: JSON!) {
          change_multiple_column_values(item_id: $item_id, board_id: $board_id, column_values: $column_values) {
            id
          }
        }
        """
        variables = {
            "item_id": int(item_id),
            "board_id": int(self.board_id),
            "column_values": json.dumps(col_vals),
        }
        await self._graphql(query, variables)

    async def _create_update(self, item_id: str, body: str):
        query = """
        mutation ($item_id: ID!, $body: String!) {
          create_update(item_id: $item_id, body: $body) { id }
        }
        """
        variables = {"item_id": int(item_id), "body": body}
        await self._graphql(query, variables)

    async def create_lead(self, lead_data: dict):
        """
        UPSERT:
        - Busca por Telefono Dedupe (texto)
        - Si existe: NO crea item, solo update + actualiza Last Msg ID
        - Si no existe: crea item con Telefono Dedupe + Last Msg ID
        """
        if not self.api_key or not self.board_id:
            logger.warning("⚠️ Faltan credenciales de Monday (API Key o Board ID).")
            return

        telefono = str(lead_data.get("telefono", "")).strip()
        nombre = str(lead_data.get("nombre", "Cliente Nuevo")).strip()
        msg_id = str(lead_data.get("external_id", "")).strip()

        if not telefono:
            logger.warning("⚠️ Lead sin teléfono. No se puede deduplicar bien.")
        
        # 1) Buscar item existente por Teléfono Dedupe
        item_id = await self._find_item_by_phone_dedupe(telefono) if telefono else None

        # 2) Crear si no existe
        if not item_id:
            item_name = f"{nombre} | {telefono}" if telefono else nombre
            item_id = await self._create_item(item_name=item_name, phone=telefono, msg_id=msg_id)
            logger.info(f"✅ Lead creado en Monday: {item_name} (ID: {item_id})")
        else:
            logger.info(f"♻️ Lead existente encontrado por teléfono: {telefono} (ID: {item_id})")

            # ✅ actualizar Last Msg ID (para auditoría)
            col_vals = {}
            if self.last_msg_id_column_id and msg_id:
                col_vals[self.last_msg_id_column_id] = msg_id
            # (opcional) también asegurar Telefono Dedupe
            if self.phone_dedupe_text_column_id and telefono:
                col_vals[self.phone_dedupe_text_column_id] = telefono
            if col_vals:
                await self._update_columns(item_id, col_vals)

        # 3) Siempre guardar detalles como update
        detalles = (
            f"📞 Teléfono: {lead_data.get('telefono', 'N/A')}\n"
            f"🚛 Interés: {lead_data.get('interes', 'N/A')}\n"
            f"📅 Cita Agendada: {lead_data.get('cita', 'Pendiente')}\n"
            f"💰 Forma de Pago: {lead_data.get('pago', 'N/A')}\n"
            f"🔥 Termómetro: {lead_data.get('termometro', 'N/A')}\n"
            f"📌 Acción: {lead_data.get('accion', 'N/A')}\n"
            f"🧾 Msg ID: {msg_id or 'N/A'}"
        )

        await self._create_update(item_id, detalles)
        logger.info("✅ Detalles guardados en Monday.")

# Instancia global
monday_service = MondayService()
