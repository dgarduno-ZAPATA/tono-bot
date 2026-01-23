import os
import httpx
import json
import logging
import re

logger = logging.getLogger(__name__)

class MondayService:
    def __init__(self):
        self.api_key = os.getenv("MONDAY_API_KEY")
        self.board_id = os.getenv("MONDAY_BOARD_ID")
        self.api_url = "https://api.monday.com/v2"

        # --- CORRECCIÓN DE IDS ---
        # 1. Columna TEXTO oculta solo para buscar (Dedupe) -> Debe ser text_mkzw7xjz
        self.phone_dedupe_col_id = os.getenv("MONDAY_DEDUPE_COLUMN_ID") 
        
        # 2. Columna TEXTO para el ID del mensaje -> Debe ser text_mkzwndf
        self.last_msg_id_col_id = os.getenv("MONDAY_LAST_MSG_ID_COLUMN_ID")

        # 3. Columna TIPO PHONE (la del icono de teléfono) -> Debe ser phone_mkzwh34a
        self.phone_real_col_id = os.getenv("MONDAY_PHONE_REAL_COLUMN_ID")

    def _sanitize_phone(self, phone: str) -> str:
        """
        Limpia el teléfono para que la búsqueda sea exacta.
        Quita +, espacios, guiones. Deja solo números.
        Ej: "+52 1 55..." -> "52155..."
        """
        if not phone: return ""
        return re.sub(r'\D', '', str(phone))

    async def _graphql(self, query: str, variables: dict):
        if not self.api_key:
            raise RuntimeError("MONDAY_API_KEY no configurada")
        
        headers = {"Authorization": self.api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(self.api_url, json={"query": query, "variables": variables}, headers=headers)
        
        data = resp.json()
        if "errors" in data:
            logger.error(f"Monday API Error: {data['errors']}")
            # No lanzamos error fatal para que el bot siga funcionando, pero logueamos
        return data

    async def _find_item_by_phone(self, phone_limpio: str):
        """Busca usando la columna de TEXTO (Dedupe)"""
        if not phone_limpio or not self.phone_dedupe_col_id:
            return None

        # Usamos items_page_by_column_values para la API 2023-10+
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
        variables = {
            "board_id": int(self.board_id), 
            "col_id": self.phone_dedupe_col_id, 
            "val": phone_limpio
        }
        
        data = await self._graphql(query, variables)
        items = data.get("data", {}).get("items_page_by_column_values", {}).get("items", [])
        
        if items:
            return items[0]["id"]
        return None

    async def create_lead(self, lead_data: dict):
        # 1. PREPARAR DATOS
        raw_phone = str(lead_data.get("telefono", ""))
        phone_limpio = self._sanitize_phone(raw_phone)
        nombre = str(lead_data.get("nombre", "Lead WhatsApp")).strip()
        msg_id = str(lead_data.get("external_id", "")).strip() # ID del mensaje de Evolution API

        if not phone_limpio:
            logger.warning("⚠️ Lead sin teléfono, no se puede procesar.")
            return

        # 2. BUSCAR DUPLICADO (Lógica Find-First)
        item_id = await self._find_item_by_phone(phone_limpio)

        # 3. DEFINIR VALORES DE COLUMNAS
        col_vals = {}
        
        # Siempre aseguramos que la columna Dedupe tenga el numero limpio
        if self.phone_dedupe_col_id:
            col_vals[self.phone_dedupe_col_id] = phone_limpio
            
        # Guardamos el ID del mensaje para evitar loops
        if self.last_msg_id_col_id and msg_id:
            col_vals[self.last_msg_id_col_id] = msg_id
            
        # Guardamos en la columna real de teléfono (Formato Monday: {phone, country})
        if self.phone_real_col_id:
            col_vals[self.phone_real_col_id] = {"phone": phone_limpio, "countryShortName": "MX"}

        # 4. CREAR O ACTUALIZAR
        if not item_id:
            # --- CREAR NUEVO ---
            logger.info(f"🆕 Creando nuevo lead: {phone_limpio}")
            query_create = """
            mutation ($board_id: ID!, $name: String!, $vals: JSON!) {
                create_item (board_id: $board_id, item_name: $name, column_values: $vals) { id }
            }
            """
            # Nombre del item: "Nombre | Telefono"
            item_name_display = f"{nombre} | {phone_limpio}"
            
            vars_create = {
                "board_id": int(self.board_id),
                "name": item_name_display,
                "vals": json.dumps(col_vals)
            }
            res = await self._graphql(query_create, vars_create)
            # Obtenemos el ID nuevo para agregarle la nota abajo
            item_id = res.get("data", {}).get("create_item", {}).get("id")
            
        else:
            # --- ACTUALIZAR EXISTENTE ---
            logger.info(f"♻️ Lead ya existe (ID: {item_id}). Actualizando columnas clave.")
            query_update = """
            mutation ($item_id: ID!, $board_id: ID!, $vals: JSON!) {
                change_multiple_column_values (item_id: $item_id, board_id: $board_id, column_values: $vals) { id }
            }
            """
            vars_update = {
                "item_id": int(item_id),
                "board_id": int(self.board_id),
                "vals": json.dumps(col_vals)
            }
            await self._graphql(query_update, vars_update)

        # 5. AGREGAR NOTA (UPDATE) CON DETALLES
        # Esto siempre pasa, sea nuevo o viejo
        if item_id:
            detalles = (
                f"👤 Nombre: {nombre}\n"
                f"📞 Tel: {phone_limpio}\n"
                f"📧 Email: {lead_data.get('email', 'N/A')}\n"
                f"📝 Interés: {lead_data.get('interes', 'N/A')}\n"
                f"🆔 MsgID: {msg_id}"
            )
            query_note = """
            mutation ($item_id: ID!, $body: String!) {
                create_update (item_id: $item_id, body: $body) { id }
            }
            """
            await self._graphql(query_note, {"item_id": int(item_id), "body": detalles})

# Instancia lista para usar
monday_service = MondayService()
