import os
import asyncio
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
        self.phone_real_col_id = os.getenv("MONDAY_PHONE_COLUMN_ID")

        # 4. Columna STATUS para etapa del funnel
        self.stage_col_id = os.getenv("MONDAY_STAGE_COLUMN_ID")

        # Log de configuración
        if self.stage_col_id:
            logger.info(f"✅ Monday Stage Column configurada: {self.stage_col_id}")
        else:
            logger.warning("⚠️ MONDAY_STAGE_COLUMN_ID no configurada - funnel no actualizará estado")

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
        payload = {"query": query, "variables": variables}

        _MAX_RETRIES = 3
        for _attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=25.0) as client:
                    resp = await client.post(self.api_url, json=payload, headers=headers)

                if resp.status_code >= 500 and _attempt < _MAX_RETRIES - 1:
                    backoff = 2 ** (_attempt + 1)
                    logger.warning(f"⚠️ Monday 5xx retry {_attempt + 1}/{_MAX_RETRIES} tras {backoff}s: {resp.status_code}")
                    await asyncio.sleep(backoff)
                    continue

                data = resp.json()
                if "errors" in data:
                    logger.error(f"Monday API Error: {data['errors']}")
                return data

            except (httpx.TimeoutException, httpx.RequestError) as e:
                if _attempt < _MAX_RETRIES - 1:
                    backoff = 2 ** (_attempt + 1)
                    logger.warning(f"⚠️ Monday retry {_attempt + 1}/{_MAX_RETRIES} tras {backoff}s: {e}")
                    await asyncio.sleep(backoff)
                else:
                    raise

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

    async def create_or_update_lead(self, lead_data: dict, stage: str = None, add_note: str = None):
        """
        Crea o actualiza un lead en Monday.com con soporte de funnel.

        Etapas del funnel:
        - MENSAJE: Primer contacto
        - ENGANCHE: Cliente interactuando
        - INTENCION: Modelo/precio mencionado
        - CALIFICADO: Cita confirmada

        Args:
            lead_data: dict con telefono, nombre, interes, etc.
            stage: Etapa del funnel (opcional, solo actualiza si es "mayor")
            add_note: Nota adicional para agregar (opcional)
        """
        # 1. PREPARAR DATOS
        raw_phone = str(lead_data.get("telefono", ""))
        phone_limpio = self._sanitize_phone(raw_phone)
        nombre = str(lead_data.get("nombre", "")).strip() or "Lead WhatsApp"
        msg_id = str(lead_data.get("external_id", "")).strip()

        if not phone_limpio:
            logger.warning("⚠️ Lead sin teléfono, no se puede procesar.")
            return None

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

        # Etapa del funnel (solo si se especifica y la columna existe)
        if stage and self.stage_col_id:
            col_vals[self.stage_col_id] = {"label": stage}
            logger.info(f"📊 Configurando estado: {self.stage_col_id} = {stage}")
        elif stage and not self.stage_col_id:
            logger.warning(f"⚠️ Stage '{stage}' no aplicada - MONDAY_STAGE_COLUMN_ID no configurada")

        # 4. CREAR O ACTUALIZAR
        is_new = False
        if not item_id:
            # --- CREAR NUEVO ---
            is_new = True
            logger.info(f"🆕 Creando lead [{stage or 'SIN_ETAPA'}]: {phone_limpio}")
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
            item_id = res.get("data", {}).get("create_item", {}).get("id")

        else:
            # --- ACTUALIZAR EXISTENTE ---
            logger.info(f"♻️ Actualizando lead [{stage or 'SIN_ETAPA'}] (ID: {item_id})")

            # Solo actualizar nombre si cambió de "Lead WhatsApp" a algo real
            if nombre and nombre != "Lead WhatsApp":
                # Actualizar también el nombre del item
                query_update_name = """
                mutation ($item_id: ID!, $board_id: ID!, $name: String!, $vals: JSON!) {
                    change_multiple_column_values (item_id: $item_id, board_id: $board_id, column_values: $vals) { id }
                }
                """
                vars_update = {
                    "item_id": int(item_id),
                    "board_id": int(self.board_id),
                    "vals": json.dumps(col_vals)
                }
                await self._graphql(query_update_name, vars_update)
            elif col_vals:
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

        # 5. AGREGAR NOTA (si es nuevo o si se especifica nota adicional)
        if item_id and (is_new or add_note):
            if is_new:
                # Nota inicial con todos los datos
                detalles = (
                    f"📊 ETAPA: {stage or 'MENSAJE'}\n"
                    f"👤 Nombre: {nombre}\n"
                    f"📞 Tel: {phone_limpio}\n"
                    f"📝 Interés: {lead_data.get('interes', 'N/A')}\n"
                )
                if lead_data.get('cita'):
                    detalles += f"📅 Cita: {lead_data.get('cita')}\n"
                if lead_data.get('pago'):
                    detalles += f"💰 Pago: {lead_data.get('pago')}\n"
            else:
                # Solo la nota adicional
                detalles = add_note or f"📊 Actualizado a etapa: {stage}"

            query_note = """
            mutation ($item_id: ID!, $body: String!) {
                create_update (item_id: $item_id, body: $body) { id }
            }
            """
            await self._graphql(query_note, {"item_id": int(item_id), "body": detalles})

        return item_id

    # Mantener compatibilidad con código existente
    async def create_lead(self, lead_data: dict):
        """Wrapper de compatibilidad - crea lead en etapa CALIFICADO."""
        return await self.create_or_update_lead(lead_data, stage="CALIFICADO")

# Instancia lista para usar
monday_service = MondayService()
