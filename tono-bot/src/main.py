import os
import json
import logging
import asyncio
import tempfile
from contextlib import asynccontextmanager
from collections import deque
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic_settings import BaseSettings

# === IMPORTACIONES PROPIAS ===
from src.inventory_service import InventoryService
from src.conversation_logic import handle_message
from src.memory_store import MemoryStore
from src.monday_service import monday_service


# === 1. CONFIGURACIÓN ROBUSTA (Pydantic) ===
class Settings(BaseSettings):
    # Obligatorias
    EVOLUTION_API_URL: str
    EVOLUTION_API_KEY: str

    # Opcionales / defaults
    EVO_INSTANCE: str = "Tractosymax2"
    OWNER_PHONE: Optional[str] = None
    SHEET_CSV_URL: Optional[str] = None
    INVENTORY_REFRESH_SECONDS: int = 300

    # Logging del payload (evita logs gigantes)
    LOG_WEBHOOK_PAYLOAD: bool = True
    LOG_WEBHOOK_PAYLOAD_MAX_CHARS: int = 6000

    class Config:
        env_file = ".env"
        extra = "ignore"


try:
    settings = Settings()
except Exception as e:
    print(f"❌ FATAL: Error en configuración de variables de entorno: {e}")
    raise

# Logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("BotTractos")


# === 2. ESTADO GLOBAL EN RAM ===
class GlobalState:
    def __init__(self):
        self.http_client: Optional[httpx.AsyncClient] = None
        self.inventory: Optional[InventoryService] = None
        self.store: Optional[MemoryStore] = None

        # dedupe RAM (si llegan 2 eventos iguales rápido)
        self.processed_message_ids = deque(maxlen=4000)
        self.processed_lead_ids = deque(maxlen=8000)

        self.silenced_users: Dict[str, bool] = {}


bot_state = GlobalState()


# === 3. LIFESPAN (INICIO/CIERRE) ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando BotTractos...")

    # A) Cliente HTTP persistente (Evolution)
    bot_state.http_client = httpx.AsyncClient(
        base_url=settings.EVOLUTION_API_URL.rstrip("/"),
        headers={"apikey": settings.EVOLUTION_API_KEY, "Content-Type": "application/json"},
        timeout=30.0,
    )

    # B) Inventario
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    INVENTORY_PATH = os.path.join(BASE_DIR, "data", "inventory.csv")

    bot_state.inventory = InventoryService(
        INVENTORY_PATH,
        sheet_csv_url=settings.SHEET_CSV_URL,
        refresh_seconds=settings.INVENTORY_REFRESH_SECONDS,
    )

    try:
        bot_state.inventory.load(force=True)
        count = len(getattr(bot_state.inventory, "items", []) or [])
        logger.info(f"✅ Inventario cargado: {count} items.")
    except Exception as e:
        logger.error(f"⚠️ Error cargando inventario inicial: {e}")

    # C) Memoria
    bot_state.store = MemoryStore()
    try:
        bot_state.store.init()
        logger.info("✅ MemoryStore inicializado.")
    except Exception as e:
        logger.error(f"⚠️ Error iniciando MemoryStore: {e}")

    yield

    # D) Limpieza
    logger.info("🛑 Deteniendo aplicación...")
    if bot_state.http_client:
        await bot_state.http_client.aclose()
    logger.info("👋 Recursos liberados.")


app = FastAPI(lifespan=lifespan)


# === 4. UTILIDADES ===
def _clean_phone_or_jid(value: str) -> str:
    if not value:
        return ""
    return "".join([c for c in str(value) if c.isdigit()])


def _extract_user_message(msg_obj: Dict[str, Any]) -> str:
    """
    Extrae el texto del mensaje de Evolution.
    Si es audio, retorna cadena vacía para que process_single_event lo maneje.
    """
    if not isinstance(msg_obj, dict):
        return ""

    # 1. Mensaje de texto normal
    if "conversation" in msg_obj:
        return msg_obj.get("conversation") or ""

    # 2. Mensaje de texto extendido (reply, etc)
    if "extendedTextMessage" in msg_obj:
        ext = msg_obj.get("extendedTextMessage") or {}
        return ext.get("text") or ""

    # 3. Imagen con caption
    if "imageMessage" in msg_obj:
        img = msg_obj.get("imageMessage") or {}
        return img.get("caption") or "📷 (Envió una foto)"

    # 4. AUDIO/NOTA DE VOZ - Retornamos vacío para señalar que hay audio
    if "audioMessage" in msg_obj or "pttMessage" in msg_obj:
        return ""  # ← Señal de que hay audio

    return ""


def _ensure_inventory_loaded() -> None:
    """
    Compatibilidad con distintas versiones de InventoryService.
    """
    inv = bot_state.inventory
    if not inv:
        return
    try:
        if hasattr(inv, "ensure_loaded"):
            inv.ensure_loaded()  # type: ignore[attr-defined]
        else:
            inv.load(force=False)  # type: ignore[arg-type]
    except Exception as e:
        logger.error(f"⚠️ No se pudo refrescar inventario: {e}")


def _safe_log_payload(prefix: str, obj: Any) -> None:
    """
    Log controlado para no llenar Render de JSON gigantes.
    """
    if not settings.LOG_WEBHOOK_PAYLOAD:
        return
    try:
        raw = json.dumps(obj, ensure_ascii=False)
        if len(raw) > settings.LOG_WEBHOOK_PAYLOAD_MAX_CHARS:
            raw = raw[: settings.LOG_WEBHOOK_PAYLOAD_MAX_CHARS] + " ...[TRUNCATED]"
        logger.info(f"{prefix}{raw}")
    except Exception as e:
        logger.warning(f"⚠️ No se pudo loggear payload: {e}")


# === 5. TRANSCRIPCIÓN DE AUDIO (🎤 WHISPER) - VERSIÓN CORREGIDA ===
async def _handle_audio_transcription(msg_id: str, remote_jid: str) -> str:
    """
    Descarga el audio DESENCRIPTADO desde Evolution API y lo transcribe con Whisper.
    
    Args:
        msg_id: ID del mensaje de audio
        remote_jid: JID del chat remoto
    
    Returns:
        Texto transcrito o cadena vacía si falla
    """
    if not msg_id or not remote_jid:
        logger.warning("⚠️ msg_id o remote_jid vacío")
        return ""

    temp_path = None
    try:
        # 1. Crear archivo temporal
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
            temp_path = temp_audio.name

        logger.info(f"⬇️ Descargando audio desde Evolution API...")

        # 2. CLAVE: Usar el endpoint de Evolution para descargar el audio desencriptado
        client = bot_state.http_client
        if not client:
            logger.error("❌ Cliente HTTP no inicializado")
            return ""

        # Endpoint de Evolution para descargar media desencriptado
        media_url = f"/chat/getBase64FromMediaMessage/{settings.EVO_INSTANCE}"
        
        payload = {
            "message": {
                "key": {
                    "remoteJid": remote_jid,
                    "id": msg_id,
                    "fromMe": False
                }
            },
            "convertToMp4": False  # Mantener en formato original (ogg)
        }

        response = await client.post(media_url, json=payload)
        
        if response.status_code != 200 and response.status_code != 201:
            logger.error(f"❌ Error descargando desde Evolution: {response.status_code} - {response.text}")
            return ""

        data = response.json()
        
        # Evolution devuelve el audio en base64
        import base64
        
        if isinstance(data, dict):
            base64_audio = data.get("base64") or data.get("media")
        else:
            base64_audio = data
            
        if not base64_audio:
            logger.error("❌ No se recibió base64 de Evolution")
            return ""

        # Decodificar y guardar
        audio_bytes = base64.b64decode(base64_audio)
        
        with open(temp_path, "wb") as f:
            f.write(audio_bytes)

        logger.info(f"✅ Audio descargado y desencriptado: {temp_path} ({len(audio_bytes)} bytes)")

        # 3. Transcribir con OpenAI Whisper
        try:
            from src.conversation_logic import client as openai_client
            
            with open(temp_path, "rb") as audio_file:
                transcript = await run_in_threadpool(
                    lambda: openai_client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        language="es",
                        response_format="text"
                    )
                )
            
            if isinstance(transcript, str):
                texto = transcript.strip()
            else:
                texto = (getattr(transcript, "text", "") or "").strip()
            
            if texto:
                logger.info(f"🎤 Audio transcrito: '{texto[:150]}...'")
            else:
                logger.warning("⚠️ Transcripción vacía")
            
            return texto

        except Exception as e:
            logger.error(f"❌ Error en Whisper API: {e}")
            return ""

    except Exception as e:
        logger.error(f"❌ Error general procesando audio: {e}")
        return ""

    finally:
        # 4. Limpieza
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                logger.info(f"🗑️ Archivo temporal eliminado")
            except Exception as e:
                logger.warning(f"⚠️ No se pudo eliminar temp file: {e}")


# === 6. ENVÍO DE MENSAJES (OPTIMIZADO PARA MÚLTIPLES FOTOS) ===
async def send_evolution_message(number_or_jid: str, text: str, media_urls: Optional[List[str]] = None):
    media_urls = media_urls or []
    text = (text or "").strip()

    if not text and not media_urls:
        return

    clean_number = _clean_phone_or_jid(number_or_jid)
    if not clean_number:
        logger.error(f"❌ No se pudo limpiar número/jid: {number_or_jid}")
        return

    client = bot_state.http_client
    if not client:
        logger.error("❌ Cliente HTTP no inicializado (lifespan).")
        return

    try:
        if media_urls:
            total_fotos = len(media_urls)
            for i, media_url in enumerate(media_urls):
                url = f"/message/sendMedia/{settings.EVO_INSTANCE}"
                
                caption_part = text if (i == total_fotos - 1) else ""
                
                payload = {
                    "number": clean_number,
                    "mediatype": "image",
                    "mimetype": "image/jpeg",
                    "caption": caption_part,
                    "media": media_url,
                }
                
                if i > 0:
                    await asyncio.sleep(0.5)

                response = await client.post(url, json=payload)
                
                if response.status_code >= 400:
                    logger.error(f"⚠️ Error foto {i+1}: {response.text}")
                else:
                    logger.info(f"✅ Enviada foto {i+1}/{total_fotos} a {clean_number}")

        else:
            url = f"/message/sendText/{settings.EVO_INSTANCE}"
            payload = {"number": clean_number, "text": text}
            response = await client.post(url, json=payload)
            
            if response.status_code >= 400:
                logger.error(f"⚠️ Error Evolution API ({response.status_code}): {response.text}")
            else:
                logger.info(f"✅ Enviado a {clean_number} (TEXT)")

    except httpx.RequestError as e:
        logger.error(f"❌ Error de conexión: {e}")
    except Exception as e:
        logger.error(f"❌ Error inesperado: {e}")


# === 7. ALERTAS AL DUEÑO ===
async def notify_owner(user_number_or_jid: str, user_message: str, bot_reply: str, is_lead: bool = False):
    if not settings.OWNER_PHONE:
        return

    clean_client = _clean_phone_or_jid(user_number_or_jid)

    if is_lead:
        alert_text = (
            "🚨 *NUEVO LEAD EN MONDAY* 🚨\n\n"
            f"Cliente: wa.me/{clean_client}\n"
            "El bot cerró una cita. ¡Revisa el tablero!"
        )
        await send_evolution_message(settings.OWNER_PHONE, alert_text)
        return

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
    await send_evolution_message(settings.OWNER_PHONE, alert_text)


# === 8. PROCESADOR CENTRAL ===
async def process_single_event(data: Dict[str, Any]):
    key = data.get("key", {}) or {}
    remote_jid = (key.get("remoteJid", "") or "").strip()
    from_me = key.get("fromMe", False)
    msg_id = (key.get("id", "") or "").strip()

    if not remote_jid:
        return

    logger.info(f"📩 Evento recibido. msg_id={msg_id} remote_jid={remote_jid}")

    if from_me:
        return

    if remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
        return

    if msg_id:
        if msg_id in bot_state.processed_message_ids:
            logger.info(f"🔁 Mensaje duplicado ignorado (RAM): {msg_id}")
            return
        bot_state.processed_message_ids.append(msg_id)

    # === EXTRACCIÓN DE MENSAJE (TEXTO O AUDIO) ===
    msg_obj = data.get("message", {}) or {}
    
    user_message = _extract_user_message(msg_obj).strip()
    
    # Si NO hay texto, verificar si es audio
    if not user_message:
        audio_info = msg_obj.get("audioMessage") or msg_obj.get("pttMessage") or {}
        
        # Verificar si hay audio (cualquier campo indica audio)
        has_audio = bool(audio_info and (
            audio_info.get("url") or 
            audio_info.get("directPath") or 
            audio_info.get("mediaKey")
        ))
        
        if has_audio:
            logger.info(f"🎤 Audio detectado. Procesando...")
            
            await send_evolution_message(remote_jid, "🎧 Escuchando tu audio...")
            
            # Transcribir usando msg_id y remote_jid
            user_message = await _handle_audio_transcription(msg_id, remote_jid)
            
            if not user_message:
                await send_evolution_message(
                    remote_jid, 
                    "🙉 Tuve un problema escuchando el audio. ¿Me lo puedes escribir o mandar de nuevo?"
                )
                return
            
            logger.info(f"✅ Transcripción exitosa. Procesando como texto...")

    if not user_message:
        return

    # --- comandos ---
    if user_message.lower() == "/silencio":
        bot_state.silenced_users[remote_jid] = True
        await send_evolution_message(remote_jid, "🔇 Bot desactivado. Un asesor humano te atenderá en breve.")

        if settings.OWNER_PHONE:
            clean_client = remote_jid.split("@")[0]
            alerta = (
                "⚠️ *HANDOFF ACTIVADO*\n\n"
                f"El chat con wa.me/{clean_client} ha sido pausado.\n"
                "El bot NO responderá hasta que el cliente envíe '/activar'."
            )
            await send_evolution_message(settings.OWNER_PHONE, alerta)
        return

    if user_message.lower() == "/activar":
        bot_state.silenced_users.pop(remote_jid, None)
        await send_evolution_message(remote_jid, "✅ Bot activado de nuevo. ¿En qué te ayudo?")
        return

    if bot_state.silenced_users.get(remote_jid) is True:
        return

    _ensure_inventory_loaded()

    store = bot_state.store
    if not store:
        logger.error("❌ MemoryStore no inicializado.")
        return

    session = store.get(remote_jid) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    try:
        result = await run_in_threadpool(handle_message, user_message, bot_state.inventory, state, context)
    except Exception as e:
        logger.error(f"❌ Error IA: {e}")
        result = {
            "reply": "Dame un momento...",
            "new_state": state,
            "context": context,
            "media_urls": [],
            "lead_info": None
        }

    reply_text = (result.get("reply") or "").strip()
    media_urls = result.get("media_urls") or []
    lead_info = result.get("lead_info")

    try:
        store.upsert(
            remote_jid,
            str(result.get("new_state", state)),
            dict(result.get("context", context)),
        )
    except Exception as e:
        logger.error(f"⚠️ Error guardando memoria: {e}")

    await send_evolution_message(remote_jid, reply_text, media_urls)

    if lead_info:
        try:
            lead_key = f"{remote_jid}|{msg_id}|lead"
            if lead_key in bot_state.processed_lead_ids:
                logger.info(f"🧱 Lead duplicado bloqueado (RAM): {lead_key}")
                return
            bot_state.processed_lead_ids.append(lead_key)

            lead_info["telefono"] = remote_jid.split("@")[0]
            lead_info["external_id"] = msg_id

            logger.info(f"🚀 ¡LEAD DETECTADO! Enviando a Monday: {lead_info.get('nombre')}")
            await monday_service.create_lead(lead_info)

            await notify_owner(remote_jid, user_message, reply_text, is_lead=True)
        except Exception as e:
            logger.error(f"❌ Error enviando LEAD a Monday: {e}")
    else:
        await notify_owner(remote_jid, user_message, reply_text, is_lead=False)


# === 9. ENDPOINTS ===
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "instance": settings.EVO_INSTANCE,
        "inventory_count": len(getattr(bot_state.inventory, "items", []) or []),
        "silenced_chats": len(bot_state.silenced_users),
        "processed_msgs_cache": len(bot_state.processed_message_ids),
        "processed_leads_cache": len(bot_state.processed_lead_ids),
    }


async def _background_process_events(events: List[Dict[str, Any]]):
    for event in events:
        try:
            await process_single_event(event)
        except Exception as e:
            logger.error(f"❌ Error procesando evento en background: {e}")


@app.post("/webhook")
async def evolution_webhook(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"❌ webhook: JSON inválido: {e}")
        return {"status": "ignored", "reason": "invalid_json"}

    _safe_log_payload("🧾 WEBHOOK PAYLOAD: ", body)

    try:
        data_payload = body.get("data")
        if not data_payload:
            return {"status": "ignored", "reason": "no_data"}

        events = data_payload if isinstance(data_payload, list) else [data_payload]

        asyncio.create_task(_background_process_events(events))
        return {"status": "accepted"}

    except Exception as e:
        logger.error(f"❌ webhook: ERROR GENERAL: {e}")
        return {"status": "error_but_acked"}
