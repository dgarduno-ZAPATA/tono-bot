import os
import httpx # Necesitaremos esta librería para enviar los mensajes
import logging
from fastapi import FastAPI, Request

from src.inventory_service import InventoryService
from src.conversation_logic import handle_message
from src.memory_store import MemoryStore

# Configuración de Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# === CONFIGURACIÓN EVOLUTION API ===
# Estas variables las tienes que poner en tus Environment Variables de Render/EasyPanel
EVO_API_URL = os.getenv("EVOLUTION_API_URL") # Ej: https://evolutionapi...easypanel.host
EVO_API_KEY = os.getenv("EVOLUTION_API_KEY") # Tu API Key que empieza con 9398...
EVO_INSTANCE = "Tractosymax2" 

# === INVENTARIO: local + Sheet ===
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY_PATH = os.path.join(BASE_DIR, "data", "inventory.csv")

SHEET_CSV_URL = os.getenv("SHEET_CSV_URL")
REFRESH_SECONDS = int(os.getenv("INVENTORY_REFRESH_SECONDS", "300"))

inventory = InventoryService(INVENTORY_PATH, sheet_csv_url=SHEET_CSV_URL, refresh_seconds=REFRESH_SECONDS)
try:
    inventory.load(force=True)
except Exception as e:
    logger.error(f"Error cargando inventario inicial: {e}")

# === MEMORIA (SQLite) ===
store = MemoryStore()
store.init()

@app.get("/health")
def health():
    return {"status": "ok"}

# --- FUNCIÓN PARA ENVIAR MENSAJE A EVOLUTION ---
async def send_evolution_message(number: str, text: str, media_urls: list = None):
    if not text and not media_urls:
        return

    url = f"{EVO_API_URL}/message/sendText/{EVO_INSTANCE}"
    
    # Limpiamos el número (solo números)
    clean_number = ''.join(filter(str.isdigit, str(number)))
    
    # Si hay media_urls, por simplicidad las agregamos al final del texto por ahora
    # (Para enviar imágenes reales se usa otro endpoint /message/sendMedia)
    if media_urls:
        text += "\n\n" + "\n".join(media_urls)

    payload = {
        "number": clean_number,
        "text": text
    }
    
    headers = {
        "apikey": EVO_API_KEY,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            logger.info(f"Enviado a Evolution: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Error enviando mensaje a Evolution: {e}")

# --- WEBHOOK NUEVO (JSON en lugar de Form) ---
@app.post("/webhook") # Ojo: Cambia la URL en Evolution a /webhook
async def evolution_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    # 1. Validar estructura de Evolution API v2
    data = body.get("data")
    if not data:
        return {"status": "ignored", "reason": "no data"}

    key = data.get("key", {})
    remote_jid = key.get("remoteJid", "")
    from_me = key.get("fromMe", False)

    # 2. EVITAR BUCLE INFINITO (Importante)
    if from_me:
        return {"status": "ignored", "reason": "from_me is true"}

    # 3. Extraer mensaje de texto (Evolution manda esto en diferentes lugares)
    user_message = ""
    msg_obj = data.get("message", {})
    
    if "conversation" in msg_obj:
        user_message = msg_obj["conversation"]
    elif "extendedTextMessage" in msg_obj and "text" in msg_obj["extendedTextMessage"]:
        user_message = msg_obj["extendedTextMessage"]["text"]
    
    # Si es imagen o audio sin texto, lo ignoramos o ponemos un placeholder
    if not user_message:
         # Intenta buscar caption si es imagen
        if "imageMessage" in msg_obj:
             user_message = msg_obj["imageMessage"].get("caption", "")
        
    user_message = (user_message or "").strip()
    
    # Si después de todo no hay texto, no procesamos (o podrías manejarlo diferente)
    if not user_message:
        return {"status": "ignored", "reason": "no text found"}

    # 4. LÓGICA ORIGINAL DE TU BOT (Intacta)
    inventory.ensure_loaded()

    session = store.get(remote_jid) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    try:
        # Aquí usamos tu lógica original
        result = handle_message(user_message, inventory, state, context)
    except Exception as e:
        logger.error(f"Error en handle_message: {e}")
        result = {"reply": "Tuve un detalle técnico 🙏 ¿Buscas auto, pickup/camioneta o camión?", "new_state": state, "context": context}

    # Procesar respuesta
    reply_text = (result.get("reply") or "").strip() if isinstance(result, dict) else ""
    if not reply_text:
        reply_text = "¿Qué modelo te interesa o buscas auto, pickup/camioneta o camión?"

    new_state = result.get("new_state", state) if isinstance(result, dict) else state
    new_context = result.get("context", context) if isinstance(result, dict) else context
    media_urls = result.get("media_urls", []) if isinstance(result, dict) else []

    # Guardar memoria
    store.upsert(remote_jid, str(new_state), dict(new_context))

    # 5. ENVIAR RESPUESTA VÍA EVOLUTION API
    await send_evolution_message(remote_jid, reply_text, media_urls)

    return {"status": "success"}
