import os
from fastapi import FastAPI, Request
from fastapi.responses import Response

from src.inventory_service import InventoryService
from src.conversation_logic import handle_message

app = FastAPI()

# Ruta al inventario
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY_PATH = os.path.join(BASE_DIR, "data", "inventory.csv")

inventory = InventoryService(INVENTORY_PATH)
inventory.load()

@app.get("/health")
def health():
    return {"status": "ok"}

def twiml(message: str) -> str:
    safe = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'

@app.post("/twilio/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    user_message = (form.get("Body") or "").strip()

    reply = handle_message(user_message, inventory)

    return Response(content=twiml(reply), media_type="application/xml")
