import os
import httpx
import json
import logging

logger = logging.getLogger(__name__)

class MondayService:
    def __init__(self):
        # Leemos las llaves que pondrás en Render
        self.api_key = os.getenv("MONDAY_API_KEY")
        self.board_id = os.getenv("MONDAY_BOARD_ID")
        self.api_url = "https://api.monday.com/v2"

    async def create_lead(self, lead_data: dict):
        """
        Crea un lead en Monday.com.
        
        Estrategia A PRUEBA DE ERRORES:
        1. Creamos el renglón (Item) con el Nombre del cliente.
        2. Agregamos una nota (Update) con todos los detalles (Teléfono, Cita, Interés).
        
        Esto evita que falle si no sabes los "IDs" exactos de las columnas en Monday.
        """
        if not self.api_key or not self.board_id:
            logger.warning("⚠️ Faltan credenciales de Monday (API Key o Board ID).")
            return

        # 1. Definimos el nombre del Item (Renglón)
        item_name = lead_data.get("nombre", "Cliente Nuevo")
        
        # Query de GraphQL para crear el item
        query_create = """
        mutation ($boardId: ID!, $itemName: String!) {
          create_item (board_id: $boardId, item_name: $itemName) {
            id
          }
        }
        """
        
        variables_create = {
            "boardId": int(self.board_id),
            "itemName": item_name
        }

        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient() as client:
            try:
                # PASO A: Crear el renglón
                resp = await client.post(
                    self.api_url,
                    json={"query": query_create, "variables": variables_create},
                    headers=headers
                )
                data = resp.json()
                
                # Verificamos errores
                if "errors" in data:
                    logger.error(f"❌ Error Monday Create: {data['errors']}")
                    return

                # Obtenemos el ID del nuevo renglón creado
                new_item_id = data["data"]["create_item"]["id"]
                logger.info(f"✅ Lead creado en Monday: {item_name} (ID: {new_item_id})")

                # PASO B: Escribir los detalles en la burbuja de comentarios (Update)
                detalles = (
                    f"📞 Teléfono: {lead_data.get('telefono', 'N/A')}\n"
                    f"🚛 Interés: {lead_data.get('interes', 'N/A')}\n"
                    f"📅 Cita Agendada: {lead_data.get('cita', 'Pendiente')}\n"
                    f"💰 Forma de Pago: {lead_data.get('pago', 'N/A')}"
                )
                
                query_update = """
                mutation ($itemId: ID!, $body: String!) {
                  create_update (item_id: $itemId, body: $body) {
                    id
                  }
                }
                """
                
                await client.post(
                    self.api_url,
                    json={"query": query_update, "variables": {"itemId": int(new_item_id), "body": detalles}},
                    headers=headers
                )
                logger.info("✅ Detalles guardados en Monday.")

            except Exception as e:
                logger.error(f"❌ Excepción conectando a Monday: {e}")

# Creamos la instancia para usarla en main.py
monday_service = MondayService()
