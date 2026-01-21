import os
import httpx
import logging
import asyncio
from collections import deque
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool # Para no bloquear con la IA

from src.inventory_service import InventoryService
from src.conversation_logic import handle_message
from src.memory_store import MemoryStore

# === 1. VALIDACIÓN DE ENTORNO (Para que no falle silenciosamente) ===
EVO_API_URL = os.getenv("EVOLUTION_API_URL")
EVO_API_KEY = os.getenv("EVOLUTION_API_KEY")
OWNER_PHONE = os.getenv("OWNER_PHONE") # Tu número para alertas

if not EVO_API_URL or not EVO_API_KEY:
    raise ValueError("❌ ERROR FATAL: Faltan EVOLUTION_API_URL o EVOLUTION_API_KEY en las variables de entorno.")

EVO_INSTANCE = "Tractosymax2"

# === 2. CONFIGURACIÓN Y LOGS ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BotTractos")

app = FastAPI()

# === 3. SERVICIOS ===
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

# === 4. SISTEMA DE DEDUPLICACIÓN (Memoria Volátil) ===
# Guardamos los últimos 1000 IDs de mensajes procesados para evitar duplicados si Evolution reintenta.
processed_message_ids = deque(maxlen=1000)

@app.get("/health")
def health():
    return {"status": "ok", "inventory_items": len(inventory.items)}

# --- FUNCIÓN DE ENVÍO ROBUSTA ---
async def send_evolution_message(number: str, text: str, media_urls: list = None):
    if not text and not media_urls:
        return

    # Limpieza final del número (solo dígitos)
    clean_number = ''.join(filter(str.isdigit, str(number)))
    
    headers = {
        "apikey": EVO_API_KEY,
        "Content-Type": "application/json"
    }

    # Timeout de 20s para que no se quede colgado
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            if media_urls and len(media_urls) > 0:
                # Enviar IMAGEN
                url = f"{EVO_API_URL}/message/sendMedia/{EVO_INSTANCE}"
                payload = {
                    "number": clean_number,
                    "mediaMessage": {
                        "mediatype": "image",
                        "caption": text,
                        "media": media_urls[0] # Primera foto
                    }
                }
                # Si hay más fotos, las pegamos como texto extra (simple hack)
                if len(media_urls) > 1:
                    payload["mediaMessage"]["caption"] += "\n\n(Más fotos disponibles en inventario)"
            else:
                # Enviar TEXTO
                url = f"{EVO_API_URL}/message/sendText/{EVO_INSTANCE}"
                payload = {
                    "number": clean_number,
                    "text": text
                }

            response = await client.post(url, json=payload, headers=headers)
            
            # Loguear respuesta real de Evolution para debug
            if response.status_code >= 400:
                logger.error(f"❌ Error Evolution {response.status_code}: {response.text}")
            else:
                logger.info(f"✅ Enviado a {clean_number}: {response.status_code}")
                
        except Exception as e:
            logger.error(f"❌ Excepción enviando mensaje: {e}")

# --- ALERTA AL DUEÑO ---
async def notify_owner(user_number, user_message, bot_reply):
    if not OWNER_PHONE:
        return
        
    keywords = ["precio", "cuanto", "interesa", "verlo", "ubicacion", "dónde", "donde", "trato", "comprar"]
    msg_lower = user_message.lower()
    
    if any(word in msg_lower for word in keywords):
        # Limpiamos el número del cliente para que el dueño pueda darle click
        clean_client = ''.join(filter(str.isdigit, str(user_number)))
        alert_text = (
            f"🔔 *ALERTA DE VENTA*\n\n"
            f"Cliente: wa.me/{clean_client}\n"
            f"Dijo: \"{user_message}\"\n"
            f"Bot respondió: \"{bot_reply[:40]}...\""
        )
        await send_evolution_message(OWNER_PHONE, alert_text)

# --- PROCESAMIENTO DE UN MENSAJE INDIVIDUAL ---
async def process_single_event(data):
    key = data.get("key", {})
    remote_jid = key.get("remoteJid", "")
    from_me = key.get("fromMe", False)
    msg_id = key.get("id", "")

    # 1. Filtros de Seguridad
    if from_me: 
        return # Es mensaje mío
    if remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
        logger.info(f"Ignorando grupo/broadcast: {remote_jid}")
        return # Ignoramos grupos

    # 2. Deduplicación
    if msg_id in processed_message_ids:
        logger.info(f"Mensaje duplicado ignorado: {msg_id}")
        return
    processed_message_ids.append(msg_id)

    # 3. Extraer Mensaje
    user_message = ""
    msg_obj = data.get("message", {})
    
    if "conversation" in msg_obj:
        user_message = msg_obj["conversation"]
    elif "extendedTextMessage" in msg_obj and "text" in msg_obj["extendedTextMessage"]:
        user_message = msg_obj["extendedTextMessage"]["text"]
    elif "imageMessage" in msg_obj:
        user_message = msg_obj["imageMessage"].get("caption", "")
        # FIX: Si manda foto sin caption, le ponemos texto default
        if not user_message:
            user_message = "📷 (Envió una foto)"

    user_message = (user_message or "").strip()
    if not user_message:
        return

    # 4. Lógica del Bot (Inventory + AI)
    # run_in_threadpool evita que la IA bloquee el servidor si hay muchos usuarios
    inventory.ensure_loaded()
    
    session = store.get(remote_jid) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    try:
        # Ejecutamos handle_message en un hilo separado (importante para performance)
        result = await run_in_threadpool(handle_message, user_message, inventory, state, context)
    except Exception as e:
        logger.error(f"Error en lógica de IA: {e}")
        result = {"reply": "Dame un momento, estoy actualizando info...", "new_state": state}

    reply_text = (result.get("reply") or "").strip()
    media_urls = result.get("media_urls", [])

    # Guardar estado
    store.upsert(remote_jid, str(result.get("new_state", state)), dict(result.get("context", context)))

    # 5. Responder y Notificar
    await send_evolution_message(remote_jid, reply_text, media_urls)
    await notify_owner(remote_jid, user_message, reply_text)


# --- WEBHOOK PRINCIPAL ---
@app.post("/webhook")
async def evolution_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    # FIX: Manejo de listas (Batched events)
    data_payload = body.get("data")
    
    if not data_payload:
        return {"status": "ignored", "reason": "no data"}

    # Si viene como objeto único, lo convertimos en lista para procesar igual
    if isinstance(data_payload, dict):
        events = [data_payload]
    elif isinstance(data_payload, list):
        events = data_payload
    else:
        return {"status": "ignored"}

    # Procesamos cada evento de la lista
    for event in events:
        await process_single_event(event)

    return {"status": "success"}
