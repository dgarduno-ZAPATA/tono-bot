import os
import httpx
import logging
from collections import deque
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool

# === IMPORTACIONES ===
from src.inventory_service import InventoryService
from src.conversation_logic import handle_message
from src.memory_store import MemoryStore
from src.monday_service import monday_service

# === 1. CONFIGURACIÓN Y VALIDACIÓN INICIAL ===
EVO_API_URL = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
EVO_API_KEY = os.getenv("EVOLUTION_API_KEY")
OWNER_PHONE = os.getenv("OWNER_PHONE")

if not EVO_API_URL or not EVO_API_KEY:
    logging.error("❌ FATAL: Faltan EVOLUTION_API_URL o EVOLUTION_API_KEY en variables de entorno.")

EVO_INSTANCE = os.getenv("EVO_INSTANCE", "Tractosymax2")

# Logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("BotTractos")

app = FastAPI()

# === 2. SERVICIOS ===
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY_PATH = os.path.join(BASE_DIR, "data", "inventory.csv")
SHEET_CSV_URL = os.getenv("SHEET_CSV_URL")
REFRESH_SECONDS = int(os.getenv("INVENTORY_REFRESH_SECONDS", "300"))

inventory = InventoryService(
    INVENTORY_PATH,
    sheet_csv_url=SHEET_CSV_URL,
    refresh_seconds=REFRESH_SECONDS
)

try:
    inventory.load(force=True)
except Exception as e:
    logger.error(f"⚠️ Error cargando inventario inicial: {e}")

store = MemoryStore()
store.init()

# === 3. CONTROL DE ESTADO (Deduplicación y Silencio) ===
processed_message_ids = deque(maxlen=1000)
silenced_users: Dict[str, bool] = {}  # remote_jid -> True


@app.get("/health")
def health():
    return {
        "status": "ok",
        "inventory_count": len(getattr(inventory, "items", []) or []),
        "silenced_chats": len(silenced_users)
    }


def _clean_phone_or_jid(value: str) -> str:
    """
    Evolution suele aceptar número en formato digits.
    remote_jid puede venir como '521XXXXXXXXXX@s.whatsapp.net'.
    """
    if not value:
        return ""
    return "".join([c for c in str(value) if c.isdigit()])


# --- ENVÍO DE MENSAJES (CON FIX DE FOTOS) ---
async def send_evolution_message(number_or_jid: str, text: str, media_urls: Optional[List[str]] = None):
    media_urls = media_urls or []
    text = (text or "").strip()

    # Si no hay nada que enviar, se sale
    if not text and not media_urls:
        return

    clean_number = _clean_phone_or_jid(number_or_jid)

    if not clean_number:
        logger.error(f"❌ No se pudo limpiar número/jid: {number_or_jid}")
        return

    headers = {
        "apikey": EVO_API_KEY,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            # CASO 1: Intentar enviar IMAGEN (Si hay)
            if media_urls:
                url = f"{EVO_API_URL}/message/sendMedia/{EVO_INSTANCE}"

                caption_full = text or ""  # caption puede ir vacío
                if len(media_urls) > 1:
                    caption_full = (caption_full + "\n\n(Más fotos disponibles en inventario)").strip()

                payload = {
                    "number": clean_number,
                    "mediatype": "image",
                    "mimetype": "image/jpeg",
                    "caption": caption_full,
                    "media": media_urls[0],
                }

                response = await client.post(url, json=payload, headers=headers)

                if response.status_code < 400:
                    logger.info(f"✅ FOTO enviada a {clean_number}")
                    return

                logger.error(f"⚠️ Falló FOTO (Error {response.status_code}), intentando TEXTO. Body: {response.text}")

            # CASO 2: Enviar SOLO TEXTO (Respaldo o Default)
            if text:
                url = f"{EVO_API_URL}/message/sendText/{EVO_INSTANCE}"
                payload = {"number": clean_number, "text": text}
                response = await client.post(url, json=payload, headers=headers)

                if response.status_code >= 400:
                    logger.error(f"❌ Error Evolution Texto {response.status_code}: {response.text}")
                else:
                    logger.info(f"✅ TEXTO enviado a {clean_number}")

        except Exception as e:
            logger.error(f"❌ Excepción enviando mensaje: {e}")


# --- ALERTA AL DUEÑO (ACTUALIZADA PARA LEADS) ---
async def notify_owner(user_number_or_jid: str, user_message: str, bot_reply: str, is_lead: bool = False):
    if not OWNER_PHONE:
        return

    clean_client = _clean_phone_or_jid(user_number_or_jid)

    if is_lead:
        alert_text = (
            "🚨 *NUEVO LEAD EN MONDAY* 🚨\n\n"
            f"Cliente: wa.me/{clean_client}\n"
            "El bot cerró una cita. ¡Revisa el tablero!"
        )
        await send_evolution_message(OWNER_PHONE, alert_text)
        return

    # Alerta Normal de Interés
    keywords = [
        "precio", "cuanto", "cuánto", "interesa", "verlo", "ubicacion", "ubicación",
        "dónde", "donde", "trato", "comprar", "informes", "info"
    ]

    msg_lower = (user_message or "").lower()
    if not any(word in msg_lower for word in keywords):
        return

    alert_text = (
        "🔔 *Interés Detectado*\n"
        f"Cliente: wa.me/{clean_client}\n"
        f"Dijo: \"{user_message}\"\n"
        f"Bot: \"{(bot_reply or '')[:60]}...\""
    )

    await send_evolution_message(OWNER_PHONE, alert_text)


def _extract_user_message(msg_obj: Dict[str, Any]) -> str:
    """
    Extrae el texto del mensaje de Evolution.
    """
    if not isinstance(msg_obj, dict):
        return ""

    if "conversation" in msg_obj:
        return msg_obj.get("conversation") or ""

    if "extendedTextMessage" in msg_obj:
        ext = msg_obj.get("extendedTextMessage") or {}
        return ext.get("text") or ""

    if "imageMessage" in msg_obj:
        img = msg_obj.get("imageMessage") or {}
        return img.get("caption") or "📷 (Envió una foto)"

    return ""


def _ensure_inventory_loaded():
    """
    Compatibilidad con distintas versiones de InventoryService.
    """
    try:
        if hasattr(inventory, "ensure_loaded"):
            inventory.ensure_loaded()
        else:
            inventory.load(force=False)
    except Exception as e:
        logger.error(f"⚠️ No se pudo refrescar inventario: {e}")


# --- PROCESADOR CENTRAL ---
async def process_single_event(data: Dict[str, Any]):
    key = data.get("key", {}) or {}
    remote_jid = key.get("remoteJid", "")
    from_me = key.get("fromMe", False)
    msg_id = key.get("id", "")

    # Ignorar mensajes enviados por el mismo bot
    if from_me:
        return

    # Ignorar grupos y broadcast
    if remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
        return

    # Deduplicación
    if msg_id and msg_id in processed_message_ids:
        logger.info(f"Mensaje duplicado ignorado: {msg_id}")
        return
    if msg_id:
        processed_message_ids.append(msg_id)

    # 1. Extraer mensaje
    msg_obj = data.get("message", {}) or {}
    user_message = _extract_user_message(msg_obj).strip()
    if not user_message:
        return

# 2. === COMANDOS DE SILENCIO (HANDOFF) ===
    if user_message.lower() == "/silencio":
        silenced_users[remote_jid] = True
        
        # 1. Avisar al cliente
        await send_evolution_message(remote_jid, "🔇 Bot desactivado. Un asesor humano te atenderá en breve.")
        
        # 2. AVISAR AL DUEÑO (¡NUEVO!) 🚨
        if OWNER_PHONE:
            clean_client = remote_jid.split("@")[0]
            alerta = f"⚠️ *HANDOFF ACTIVADO*\n\nEl chat con wa.me/{clean_client} ha sido pausado.\nEl bot NO responderá hasta que envíes '/activar'."
            await send_evolution_message(OWNER_PHONE, alerta)
            
        return

    # 3. Lógica del Bot (Adrian)
    _ensure_inventory_loaded()

    session = store.get(remote_jid) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    try:
        result = await run_in_threadpool(handle_message, user_message, inventory, state, context)
    except Exception as e:
        logger.error(f"❌ Error IA: {e}")
        result = {"reply": "Dame un momento...", "new_state": state, "context": context, "media_urls": [], "lead_info": None}

    reply_text = (result.get("reply") or "").strip()
    media_urls = result.get("media_urls") or []
    lead_info = result.get("lead_info")

    # 4. Actualizar memoria
    try:
        store.upsert(
            remote_jid,
            str(result.get("new_state", state)),
            dict(result.get("context", context)),
        )
    except Exception as e:
        logger.error(f"⚠️ Error guardando memoria: {e}")

    # 5. Responder al cliente
    await send_evolution_message(remote_jid, reply_text, media_urls)

    # 6. GESTIÓN DE LEAD (MONDAY + ALERTAS)
    if lead_info:
        try:
            lead_info["telefono"] = remote_jid.split("@")[0]
            logger.info(f"🚀 ¡LEAD DETECTADO! Enviando a Monday: {lead_info.get('nombre')}")
            await monday_service.create_lead(lead_info)
            await notify_owner(remote_jid, user_message, reply_text, is_lead=True)
        except Exception as e:
            logger.error(f"❌ Error enviando LEAD a Monday: {e}")
    else:
        await notify_owner(remote_jid, user_message, reply_text, is_lead=False)


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
        try:
            await process_single_event(event)
        except Exception as e:
            logger.error(f"❌ Error procesando evento: {e}")

    return {"status": "success"}

