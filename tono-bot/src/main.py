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

    from_number = (form.get("From") or "").strip()
    user_message = (form.get("Body") or "").strip()

    if not from_number:
        return Response(content=twiml("No pude identificar el número. Intenta de nuevo."), media_type="application/xml")

    # 1) Cargar memoria
    session = store.get(from_number) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    # 2) Procesar
    try:
        result = handle_message(user_message, inventory, state, context)
    except Exception:
        result = {"reply": "Tuve un detalle técnico 🙏 ¿Buscas auto, pickup/camioneta o camión?", "new_state": state, "context": context}

    # 3) Asegurar salida
    if isinstance(result, dict):
        reply_text = (result.get("reply") or "").strip() or "¿Buscas auto, pickup/camioneta o camión?"
        new_state = result.get("new_state", state)
        new_context = result.get("context", context) or context
    else:
        # Nunca mandamos JSON aunque llegue algo raro
        reply_text = "¿Buscas auto, pickup/camioneta o camión?"
        new_state = state
        new_context = context

    # 4) Guardar memoria
    store.upsert(from_number, str(new_state), dict(new_context))

    # 5) Responder a Twilio (SOLO TEXTO)
    return Response(content=twiml(reply_text), media_type="application/xml")
