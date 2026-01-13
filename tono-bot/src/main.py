import os
from fastapi import FastAPI, Request
from fastapi.responses import Response

from src.inventory_service import InventoryService
from src.conversation_logic import handle_message
from src.memory_store import MemoryStore

app = FastAPI()

# === INVENTARIO ===
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY_PATH = os.path.join(BASE_DIR, "data", "inventory.csv")

inventory = InventoryService(INVENTORY_PATH)
inventory.load()

# === MEMORIA (SQLite) ===
store = MemoryStore()
store.init()

@app.get("/health")
def health():
    return {"status": "ok"}

def twiml(message: str) -> str:
    safe = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'

@app.post("/twilio/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()

    # 1️⃣ QUIÉN ES (número de WhatsApp)
    from_number = (form.get("From") or "").strip()
    user_message = (form.get("Body") or "").strip()

    # 2️⃣ BUSCAR SU MEMORIA
    session = store.get(from_number) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {})

    # 3️⃣ RESPUESTA (IA + CONTEXTO)
    reply = handle_message(user_message, inventory, state, context)

    # 4️⃣ ACTUALIZAR MEMORIA (muy simple)
    new_state = state
    new_context = context

    txt = user_message.lower()

    if state == "start":
        new_state = "active"

    if "cita" in txt or "viernes" in txt or "mañana" in txt or "hoy" in txt:
        new_state = "booking"
        new_context["requested_appointment"] = user_message

    # 5️⃣ GUARDAR MEMORIA
    store.upsert(from_number, new_state, new_context)

    return Response(content=twiml(reply), media_type="application/xml")
