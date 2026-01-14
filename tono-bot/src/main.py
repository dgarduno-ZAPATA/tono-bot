import os
from fastapi import FastAPI, Request
from fastapi.responses import Response

from src.inventory_service import InventoryService
from src.conversation_logic import handle_message
from src.memory_store import MemoryStore

app = FastAPI()

# === INVENTARIO: local + Sheet ===
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY_PATH = os.path.join(BASE_DIR, "data", "inventory.csv")

SHEET_CSV_URL = os.getenv("SHEET_CSV_URL")  # <-- la pondremos en Render
REFRESH_SECONDS = int(os.getenv("INVENTORY_REFRESH_SECONDS", "300"))

inventory = InventoryService(INVENTORY_PATH, sheet_csv_url=SHEET_CSV_URL, refresh_seconds=REFRESH_SECONDS)
inventory.load(force=True)

# === MEMORIA (SQLite) ===
store = MemoryStore()
store.init()

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

def twiml(message: str, media_urls: list[str] | None = None) -> str:
    safe = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    media_urls = media_urls or []

    media_tags = ""
    for url in media_urls[:3]:
        u = (url or "").strip()
        if u:
            media_tags += f"<Media>{u}</Media>"

    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message><Body>{safe}</Body>{media_tags}</Message></Response>'

@app.post("/twilio/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    from_number = (form.get("From") or "").strip()
    user_message = (form.get("Body") or "").strip()

    if not from_number:
        return Response(content=twiml("No pude identificar el número. Intenta de nuevo."), media_type="application/xml")

    # ✅ refresca inventario (con cache)
    inventory.ensure_loaded()

    # memoria
    session = store.get(from_number) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    try:
        result = handle_message(user_message, inventory, state, context)
    except Exception:
        result = {"reply": "Tuve un detalle técnico 🙏 ¿Buscas auto, pickup/camioneta o camión?", "new_state": state, "context": context}

    reply_text = (result.get("reply") or "").strip() if isinstance(result, dict) else ""
    if not reply_text:
        reply_text = "¿Qué modelo te interesa o buscas auto, pickup/camioneta o camión?"

    new_state = result.get("new_state", state) if isinstance(result, dict) else state
    new_context = result.get("context", context) if isinstance(result, dict) else context

    media_urls = result.get("media_urls", []) if isinstance(result, dict) else []
    if not isinstance(media_urls, list):
        media_urls = []

    store.upsert(from_number, str(new_state), dict(new_context))

    return Response(content=twiml(reply_text, media_urls), media_type="application/xml")
