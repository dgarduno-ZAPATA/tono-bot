import os
import httpx
import logging
import asyncio
from collections import deque
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool

from src.inventory_service import InventoryService
from src.conversation_logic import handle_message
from src.memory_store import MemoryStore

# === 1. CONFIGURACIÓN Y VALIDACIÓN INICIAL ===
EVO_API_URL = os.getenv("EVOLUTION_API_URL")
EVO_API_KEY = os.getenv("EVOLUTION_API_KEY")
OWNER_PHONE = os.getenv("OWNER_PHONE")

if not EVO_API_URL or not EVO_API_KEY:
    logging.error("❌ FATAL: Faltan EVOLUTION_API_URL o EVOLUTION_API_KEY en variables de entorno.")

EVO_INSTANCE = "Tractosymax2"

# Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BotTractos")

app = FastAPI()

# === 2. SERVICIOS ===
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY_PATH = os.path.join(BASE_DIR, "data", "inventory.csv")
SHEET_CSV_URL = os.getenv("SHEET_CSV_URL")
REFRESH_SECONDS = int(os.getenv("INVENTORY_REFRESH_SECONDS", "300"))

inventory = InventoryService(INVENTORY_PATH, sheet_csv_url=SHEET_CSV_URL, refresh_seconds=REFRESH_SECONDS)
try:
    inventory.load(force=True)
except Exception as e:
    logger.error(f"⚠️ Error cargando inventario inicial: {e}")

store = MemoryStore()
store.init()

# === 3. DEDUPLICACIÓN (Memoria Volátil) ===
processed_message_ids = deque(maxlen=1000)

@app.get("/health")
def health():
    return {"status": "ok", "inventory_count": len(inventory.items)}

# --- ENVÍO DE MENSAJES CORREGIDO (FIX ERROR 400) ---
async def send_evolution_message(number: str, text: str, media_urls: list = None):
    if not text and not media_urls:
        return

    clean_number = ''.join(filter(str.isdigit, str(number)))
    headers = {
        "apikey": EVO_API_KEY,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            # CASO 1: Intentar enviar IMAGEN (Si hay)
            if media_urls and len(media_urls) > 0:
                url = f"{EVO_API_URL}/message/sendMedia/{EVO_INSTANCE}"
                
                # CORRECCIÓN AQUÍ: Quitamos 'mediaMessage' y lo ponemos plano
                caption_full = text
                if len(media_urls) > 1:
                    caption_full += "\n\n(Más fotos disponibles en inventario)"

                payload = {
                    "number": clean_number,
                    "mediatype": "image",
                    "mimetype": "image/jpeg", # Ayuda a Evolution a procesarlo mejor
                    "caption": caption_full,
                    "media": media_urls[0] 
                }

                response = await client.post(url, json=payload, headers=headers)
                
                if response.status_code < 400:
                    logger.info(f"✅ FOTO enviada a {clean_number}")
                    return # Éxito, nos vamos

                # Si falló la foto (Error 400), no nos rendimos, pasamos al CASO 2
                logger.error(f"⚠️ Falló FOTO (Error {response.status_code}), intentando solo TEXTO...")

            # CASO 2: Enviar SOLO TEXTO (Respaldo o Default)
            url = f"{EVO_API_URL}/message/sendText/{EVO_INSTANCE}"
            payload = {
                "number": clean_number,
                "text": text
            }
            response = await client.post(url, json=payload, headers=headers)
            
            if response.status_code >= 400:
                logger.error(f"❌ Error Evolution Texto {response.status_code}: {response.text}")
            else:
                logger.info(f"✅ TEXTO enviado a {clean_number}")

        except Exception as e:
            logger.error(f"❌ Excepción enviando mensaje: {e}")

# --- ALERTA AL DUEÑO ---
async def notify_owner(user_number, user_message, bot_reply):
    if not OWNER_PHONE:
        return
    
    # Palabras clave ampliadas
    keywords = ["precio", "cuanto", "interesa", "verlo", "ubicacion", "dónde", "donde", "trato", "comprar", "informes", "info"]
    msg_lower = user_message.lower()
    
    if any(word in msg_lower for word in keywords):
        clean_client = ''.join(filter(str.isdigit, str(user_number)))
        alert_text = (
            f"🔔 *ALERTA DE VENTA*\n\n"
            f"Cliente: wa.me/{clean_client}\n"
            f"Dijo: \"{user_message}\"\n"
            f"Bot: \"{bot_reply[:40]}...\""
        )
        await send_evolution_message(OWNER_PHONE, alert_text)

# --- PROCESADOR CENTRAL ---
async def process_single_event(data):
    key = data.get("key", {})
    remote_jid = key.get("remoteJid", "")
    from_me = key.get("fromMe", False)
    msg_id = key.get("id", "")

    if from_me: return
    if remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
        return 

    if msg_id in processed_message_ids:
        logger.info(f"Mensaje duplicado ignorado: {msg_id}")
        return
    processed_message_ids.append(msg_id)

    user_message = ""
    msg_obj = data.get("message", {})
    
    if "conversation" in msg_obj:
        user_message = msg_obj["conversation"]
    elif "extendedTextMessage" in msg_obj and "text" in msg_obj["extendedTextMessage"]:
        user_message = msg_obj["extendedTextMessage"]["text"]
    elif "imageMessage" in msg_obj:
        user_message = msg_obj["imageMessage"].get("caption", "")
        if not user_message:
            user_message = "📷 (Envió una foto)"

    user_message = (user_message or "").strip()
    if not user_message:
        return

    inventory.ensure_loaded()
    session = store.get(remote_jid) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    try:
        result = await run_in_threadpool(handle_message, user_message, inventory, state, context)
    except Exception as e:
        logger.error(f"Error IA: {e}")
        result = {"reply": "Dame un momento...", "new_state": state}

    reply_text = (result.get("reply") or "").strip()
    media_urls = result.get("media_urls", [])

    store.upsert(remote_jid, str(result.get("new_state", state)), dict(result.get("context", context)))

    await send_evolution_message(remote_jid, reply_text, media_urls)
    await notify_owner(remote_jid, user_message, reply_text)

# --- WEBHOOK ---
@app.post("/webhook")
async def evolution_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    data_payload = body.get("data")
    if not data_payload:
        return {"status": "ignored"}

    events = data_payload if isinstance(data_payload, list) else [data_payload]

    for event in events:
        await process_single_event(event)

    return {"status": "success"}
