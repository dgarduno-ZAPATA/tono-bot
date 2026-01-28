import os
import json
import logging
import asyncio
import tempfile
import random
import time
import re
import base64
from contextlib import asynccontextmanager
from collections import deque, OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
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

    # Handoff
    TEAM_NUMBERS: str = ""
    AUTO_REACTIVATE_MINUTES: int = 60
    HUMAN_DETECTION_WINDOW_SECONDS: int = 3

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

# Handoff: lista derivada de settings
TEAM_NUMBERS_LIST = [n.strip() for n in settings.TEAM_NUMBERS.split(",") if n.strip()]
if TEAM_NUMBERS_LIST:
    logger.info(f"✅ Números del equipo configurados: {len(TEAM_NUMBERS_LIST)}")


# === 2. ESTADO GLOBAL EN RAM ===
class BoundedOrderedSet:
    """Set con O(1) lookup y evicción FIFO al llegar al límite."""

    def __init__(self, maxlen: int):
        self._data: OrderedDict = OrderedDict()
        self._maxlen = maxlen

    def add(self, key):
        if key in self._data:
            return
        if len(self._data) >= self._maxlen:
            self._data.popitem(last=False)
        self._data[key] = None

    def __contains__(self, key):
        return key in self._data

    def __len__(self):
        return len(self._data)


class GlobalState:
    def __init__(self):
        self.http_client: Optional[httpx.AsyncClient] = None
        self.inventory: Optional[InventoryService] = None
        self.store: Optional[MemoryStore] = None

        # dedupe RAM (O(1) lookup con evicción FIFO)
        self.processed_message_ids = BoundedOrderedSet(maxlen=4000)
        self.processed_lead_ids = BoundedOrderedSet(maxlen=8000)

        # Silencios (ahora soporta timestamp o bool)
        self.silenced_users: Dict[str, Any] = {}
        
        # 🆕 HANDOFF: Rastreo de mensajes del bot
        self.bot_sent_message_ids = BoundedOrderedSet(maxlen=2000)
        self.bot_sent_texts: Dict[str, deque] = {}
        self.last_bot_message_time: Dict[str, float] = {}


bot_state = GlobalState()


# === 3. LIFESPAN (INICIO/CIERRE) ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando BotTractos con sistema completo...")

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
        await bot_state.inventory.load(force=True)
        count = len(getattr(bot_state.inventory, "items", []) or [])
        logger.info(f"✅ Inventario cargado: {count} items.")
    except Exception as e:
        logger.error(f"⚠️ Error cargando inventario inicial: {e}")

    # C) Memoria
    bot_state.store = MemoryStore()
    try:
        await bot_state.store.init()
        logger.info("✅ MemoryStore inicializado.")
    except Exception as e:
        logger.error(f"⚠️ Error iniciando MemoryStore: {e}")

    yield

    # D) Limpieza
    logger.info("🛑 Deteniendo aplicación...")
    if bot_state.store:
        await bot_state.store.close()
    if bot_state.http_client:
        await bot_state.http_client.aclose()
    logger.info("👋 Recursos liberados.")


app = FastAPI(lifespan=lifespan)


# === 4. UTILIDADES ===
def _clean_phone_or_jid(value: str) -> str:
    if not value:
        return ""
    return "".join([c for c in str(value) if c.isdigit()])


def _extract_user_message(msg_obj: Dict[str, Any]) -> Tuple[str, bool]:
    """
    Extrae el texto del mensaje de Evolution.
    Retorna (texto, is_audio).
    """
    if not isinstance(msg_obj, dict):
        return "", False

    # 1. Mensaje de texto normal
    if "conversation" in msg_obj:
        return msg_obj.get("conversation") or "", False

    # 2. Mensaje de texto extendido (reply, etc)
    if "extendedTextMessage" in msg_obj:
        ext = msg_obj.get("extendedTextMessage") or {}
        return ext.get("text") or "", False

    # 3. Imagen con caption
    if "imageMessage" in msg_obj:
        img = msg_obj.get("imageMessage") or {}
        return img.get("caption") or "(Envió una foto)", False

    # 4. AUDIO/NOTA DE VOZ
    if "audioMessage" in msg_obj or "pttMessage" in msg_obj:
        return "", True

    return "", False


async def _ensure_inventory_loaded() -> None:
    """
    Compatibilidad con distintas versiones de InventoryService.
    """
    inv = bot_state.inventory
    if not inv:
        return
    try:
        if hasattr(inv, "ensure_loaded"):
            await inv.ensure_loaded()
        else:
            await inv.load(force=False)
    except Exception as e:
        logger.error(f"⚠️ No se pudo refrescar inventario: {e}")


def _safe_log_payload(prefix: str, obj: Any) -> None:
    """
    Log controlado CON SANITIZACIÓN.
    """
    if not settings.LOG_WEBHOOK_PAYLOAD:
        return
    try:
        raw = json.dumps(obj, ensure_ascii=False)
        
        # 🔒 SANITIZAR información sensible
        raw = raw.replace(settings.EVOLUTION_API_KEY, "***REDACTED***")
        raw = re.sub(r'"apikey":\s*"[^"]*"', '"apikey": "***"', raw)
        raw = re.sub(r'"password":\s*"[^"]*"', '"password": "***"', raw)
        raw = re.sub(r'"token":\s*"[^"]*"', '"token": "***"', raw)
        
        if len(raw) > settings.LOG_WEBHOOK_PAYLOAD_MAX_CHARS:
            raw = raw[: settings.LOG_WEBHOOK_PAYLOAD_MAX_CHARS] + " ...[TRUNCATED]"
        logger.info(f"{prefix}{raw}")
    except Exception as e:
        logger.warning(f"⚠️ No se pudo loggear payload: {e}")


async def _evo_post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """POST a Evolution API con retry automático en 429 (rate limit)."""
    _MAX_RETRIES = 3
    for _attempt in range(_MAX_RETRIES):
        response = await client.post(url, **kwargs)
        if response.status_code == 429 and _attempt < _MAX_RETRIES - 1:
            retry_after = response.headers.get("retry-after")
            backoff = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** (_attempt + 1)
            logger.warning(f"⚠️ Evolution 429 retry {_attempt + 1}/{_MAX_RETRIES} tras {backoff}s")
            await asyncio.sleep(backoff)
            continue
        return response
    return response


# === 5. 🆕 DETECCIÓN DE MENSAJES HUMANOS ===
def _message_looks_human(text: str) -> bool:
    """Detecta si un mensaje tiene características que el bot NO usaría."""
    if not text:
        return False
    
    text_lower = text.lower()
    
    # 1. El bot NUNCA usa emojis
    emoji_patterns = ["😊", "👍", "🙏", "💪", "🚚", "✅", "❤️", "🔥", "👌", "😉", "😅", "🤝", "📞", "📱", "🎉", "💯"]
    if any(emoji in text for emoji in emoji_patterns):
        logger.debug(f"🔍 Detectado emoji humano en: '{text[:50]}'")
        return True
    
    # 2. Frases típicas de asesor humano
    human_phrases = [
        "un momento", "déjame verificar", "déjame revisar", "te marco", "te llamo",
        "te hablo", "estoy revisando", "dame un segundo", "aquí adrian", "soy adrian",
        "con adrian", "te contacto", "te escribo", "ahora te", "espérame", "un sec"
    ]
    if any(phrase in text_lower for phrase in human_phrases):
        logger.debug(f"🔍 Detectada frase humana en: '{text[:50]}'")
        return True
    
    # 3. Errores de ortografía típicamente humanos
    typos = ["aver", "haber si", "ps si", "nel", "simon", "sisas", "ok ok", "oks"]
    if any(typo in text_lower for typo in typos):
        logger.debug(f"🔍 Detectado typo humano en: '{text[:50]}'")
        return True
    
    return False


def _is_bot_message(remote_jid: str, msg_id: str, msg_text: str) -> bool:
    """
    Verifica si un mensaje saliente fue enviado por el bot (multicapa).
    """
    # CAPA 1: Verificar ID del mensaje
    if msg_id and msg_id in bot_state.bot_sent_message_ids:
        logger.debug(f"✓ Mensaje ID {msg_id[:20]}... es del bot")
        return True
    
    # CAPA 2: Verificar texto exacto reciente
    if remote_jid in bot_state.bot_sent_texts:
        recent_texts = bot_state.bot_sent_texts[remote_jid]
        if msg_text in recent_texts:
            logger.debug(f"✓ Texto coincide con cache del bot")
            return True
    
    # CAPA 3: Verificar timestamp (ventana temporal)
    last_bot_time = bot_state.last_bot_message_time.get(remote_jid, 0)
    time_diff = time.time() - last_bot_time
    
    if time_diff < settings.HUMAN_DETECTION_WINDOW_SECONDS:
        logger.debug(f"✓ Dentro de ventana temporal ({time_diff:.1f}s)")
        return True
    
    logger.debug(f"✗ NO es del bot (time_diff={time_diff:.1f}s)")
    return False


# === 6. DELAY HUMANO ALEATORIO ===
async def human_typing_delay():
    """Simula el tiempo que un humano tarda en escribir."""
    delay = random.uniform(5.0, 10.0)
    logger.info(f"⏳ Esperando {delay:.1f}s (delay humano)...")
    await asyncio.sleep(delay)


# === 7. TRANSCRIPCIÓN DE AUDIO ===
async def _handle_audio_transcription(msg_id: str, remote_jid: str) -> str:
    """
    Descarga el audio DESENCRIPTADO desde Evolution API y lo transcribe con Whisper.
    """
    if not msg_id or not remote_jid:
        logger.warning("⚠️ msg_id o remote_jid vacío")
        return ""

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
            temp_path = temp_audio.name

        logger.info(f"⬇️ Descargando audio desde Evolution API...")

        client = bot_state.http_client
        if not client:
            logger.error("❌ Cliente HTTP no inicializado")
            return ""

        media_url = f"/chat/getBase64FromMediaMessage/{settings.EVO_INSTANCE}"
        
        payload = {
            "message": {
                "key": {
                    "remoteJid": remote_jid,
                    "id": msg_id,
                    "fromMe": False
                }
            },
            "convertToMp4": False
        }

        response = await _evo_post(client, media_url, json=payload)

        if response.status_code not in [200, 201]:
            logger.error(f"❌ Error descargando desde Evolution: {response.status_code}")
            return ""

        data = response.json()

        if isinstance(data, dict):
            base64_audio = data.get("base64") or data.get("media")
        else:
            base64_audio = data
            
        if not base64_audio:
            logger.error("❌ No se recibió base64 de Evolution")
            return ""

        audio_bytes = base64.b64decode(base64_audio)
        
        with open(temp_path, "wb") as f:
            f.write(audio_bytes)

        logger.info(f"✅ Audio descargado: {len(audio_bytes)} bytes")

        try:
            from src.conversation_logic import client as openai_client

            with open(temp_path, "rb") as audio_file:
                transcript = await openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="es",
                    response_format="text"
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
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                logger.info(f"🗑️ Archivo temporal eliminado")
            except Exception as e:
                logger.warning(f"⚠️ No se pudo eliminar temp file: {e}")


# === 8. ENVÍO DE MENSAJES (CON RASTREO) ===
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

                response = await _evo_post(client, url, json=payload)

                if response.status_code >= 400:
                    logger.error(f"⚠️ Error foto {i+1}: {response.text}")
                else:
                    logger.info(f"✅ Enviada foto {i+1}/{total_fotos} a {clean_number}")
                    
                    try:
                        resp_data = response.json()
                        msg_id = resp_data.get("key", {}).get("id")
                        if msg_id:
                            bot_state.bot_sent_message_ids.add(msg_id)
                    except Exception:
                        pass

        else:
            url = f"/message/sendText/{settings.EVO_INSTANCE}"
            payload = {"number": clean_number, "text": text}
            response = await _evo_post(client, url, json=payload)

            if response.status_code >= 400:
                logger.error(f"⚠️ Error Evolution API ({response.status_code}): {response.text}")
            else:
                logger.info(f"✅ Enviado a {clean_number} (TEXT)")
                
                jid = f"{clean_number}@s.whatsapp.net"
                
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get("key", {}).get("id")
                    if msg_id:
                        bot_state.bot_sent_message_ids.add(msg_id)
                        logger.debug(f"📤 Rastreando msg_id: {msg_id[:20]}...")
                except Exception:
                    pass

                if jid not in bot_state.bot_sent_texts:
                    bot_state.bot_sent_texts[jid] = deque(maxlen=10)
                bot_state.bot_sent_texts[jid].append(text)
                
                bot_state.last_bot_message_time[jid] = time.time()

    except httpx.RequestError as e:
        logger.error(f"❌ Error de conexión: {e}")
    except Exception as e:
        logger.error(f"❌ Error inesperado: {e}")


# === 9. ALERTAS AL DUEÑO ===
async def notify_owner(user_number_or_jid: str, user_message: str, bot_reply: str, is_lead: bool = False):
    if not settings.OWNER_PHONE:
        return

    clean_client = _clean_phone_or_jid(user_number_or_jid)

    if is_lead:
        alert_text = (
            "*NUEVO LEAD EN MONDAY*\n\n"
            f"Cliente: wa.me/{clean_client}\n"
            "El bot cerró una cita. Revisa el tablero."
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
        "*Interés Detectado*\n"
        f"Cliente: wa.me/{clean_client}\n"
        f"Dijo: \"{user_message}\"\n"
        f"Bot: \"{(bot_reply or '')[:60]}...\""
    )
    await send_evolution_message(settings.OWNER_PHONE, alert_text)


# === 10. PROCESADOR CENTRAL ===
async def process_single_event(data: Dict[str, Any]):
    key = data.get("key", {}) or {}
    remote_jid = (key.get("remoteJid", "") or "").strip()
    from_me = key.get("fromMe", False)
    msg_id = (key.get("id", "") or "").strip()

    if not remote_jid:
        return

    logger.info(f"📩 Evento: msg_id={msg_id[:20]}... from_me={from_me}")

    # Ignorar grupos/broadcast
    if remote_jid.endswith("@g.us") or "broadcast" in remote_jid:
        return

    # Deduplicación por msg_id
    if msg_id and msg_id in bot_state.processed_message_ids:
        logger.debug(f"🔁 Mensaje duplicado ignorado: {msg_id}")
        return
    
    if msg_id:
        bot_state.processed_message_ids.add(msg_id)

    # === DETECCIÓN DE HANDOFF (MENSAJE SALIENTE) ===
    if from_me:
        msg_obj = data.get("message", {}) or {}
        msg_text, _ = _extract_user_message(msg_obj)
        msg_text = msg_text.strip()
        
        # Verificar si este mensaje fue enviado por el bot
        if _is_bot_message(remote_jid, msg_id, msg_text):
            logger.debug(f"✓ Confirmado mensaje del bot, ignorando")
            return
        
        # Si NO es del bot → Es un HUMANO respondiendo
        is_human = _message_looks_human(msg_text)
        
        if is_human:
            logger.info(f"🤐 HUMANO DETECTADO en {remote_jid} (silencio por {settings.AUTO_REACTIVATE_MINUTES} min)")
            bot_state.silenced_users[remote_jid] = time.time() + (settings.AUTO_REACTIVATE_MINUTES * 60)
            return
        
        # Mensajes ambiguos: NO silenciar automáticamente
        if not msg_text:
            logger.debug(f"⏭️ Mensaje saliente vacío/sticker en {remote_jid}, ignorando")
            return
        
        logger.info(f"🤔 Mensaje saliente ambiguo en {remote_jid}, monitoreando")
        return

    # === VERIFICAR SI EL BOT ESTÁ SILENCIADO ===
    if remote_jid in bot_state.silenced_users:
        silence_value = bot_state.silenced_users[remote_jid]
        
        if isinstance(silence_value, (int, float)):
            if time.time() < silence_value:
                mins_left = int((silence_value - time.time()) / 60)
                logger.info(f"🤐 Bot silenciado en {remote_jid} ({mins_left} min restantes)")
                return
            else:
                del bot_state.silenced_users[remote_jid]
                logger.info(f"✅ Bot reactivado automáticamente en {remote_jid}")
        elif silence_value is True:
            logger.info(f"🤐 Bot silenciado permanentemente en {remote_jid}")
            return

    # === EXTRACCIÓN DE MENSAJE (TEXTO O AUDIO) ===
    msg_obj = data.get("message", {}) or {}
    user_message, is_audio = _extract_user_message(msg_obj)
    user_message = user_message.strip()

    # Si NO hay texto y es audio, transcribir
    if not user_message and is_audio:
        logger.info(f"🎤 Audio detectado, procesando...")
        user_message = await _handle_audio_transcription(msg_id, remote_jid)

        if not user_message:
            await send_evolution_message(
                remote_jid,
                "Tuve un problema escuchando el audio. ¿Me lo puedes escribir o mandar de nuevo?"
            )
            return

        logger.info(f"✅ Transcripción exitosa, procesando como texto...")

    if not user_message:
        return

    # === COMANDOS DEL CLIENTE ===
    if user_message.lower() == "/silencio":
        bot_state.silenced_users[remote_jid] = True
        await send_evolution_message(remote_jid, "Bot desactivado. Un asesor humano te atenderá en breve.")

        if settings.OWNER_PHONE:
            clean_client = remote_jid.split("@")[0]
            alerta = (
                "*HANDOFF ACTIVADO*\n\n"
                f"El chat con wa.me/{clean_client} ha sido pausado.\n"
                "El bot NO responderá hasta que el cliente envíe '/activar'."
            )
            await send_evolution_message(settings.OWNER_PHONE, alerta)
        return

    if user_message.lower() == "/activar":
        bot_state.silenced_users.pop(remote_jid, None)
        await send_evolution_message(remote_jid, "Bot activado de nuevo. ¿En qué te ayudo?")
        return

    # Refrescar inventario
    await _ensure_inventory_loaded()

    store = bot_state.store
    if not store:
        logger.error("❌ MemoryStore no inicializado.")
        return

    session = await store.get(remote_jid) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    # Delay humano aleatorio (5-10 segundos)
    await human_typing_delay()

    try:
        result = await handle_message(user_message, bot_state.inventory, state, context)
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
        await store.upsert(
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
                logger.info(f"🧱 Lead duplicado bloqueado: {lead_key}")
                return
            bot_state.processed_lead_ids.add(lead_key)

            lead_info["telefono"] = remote_jid.split("@")[0]
            lead_info["external_id"] = msg_id

            logger.info(f"🚀 LEAD DETECTADO: {lead_info.get('nombre')} - {lead_info.get('interes')}")
            await monday_service.create_lead(lead_info)

            await notify_owner(remote_jid, user_message, reply_text, is_lead=True)
        except Exception as e:
            logger.error(f"❌ Error enviando LEAD a Monday: {e}")
    else:
        await notify_owner(remote_jid, user_message, reply_text, is_lead=False)


# === 11. ENDPOINTS ===
@app.get("/health")
async def health():
    """Endpoint de salud con métricas del sistema."""
    return {
        "status": "ok",
        "instance": settings.EVO_INSTANCE,
        "inventory_count": len(getattr(bot_state.inventory, "items", []) or []),
        "silenced_chats": len(bot_state.silenced_users),
        "processed_msgs_cache": len(bot_state.processed_message_ids),
        "processed_leads_cache": len(bot_state.processed_lead_ids),
        "bot_messages_tracked": len(bot_state.bot_sent_message_ids),
        "handoff_enabled": len(TEAM_NUMBERS_LIST) > 0,
        "auto_reactivate_minutes": settings.AUTO_REACTIVATE_MINUTES,
    }


async def _background_process_events(events: List[Dict[str, Any]]):
    """Procesa eventos en background para ACK inmediato al webhook."""
    for event in events:
        try:
            await process_single_event(event)
        except Exception as e:
            logger.error(f"❌ Error procesando evento en background: {e}")


@app.post("/webhook")
async def evolution_webhook(request: Request):
    """
    Webhook anti-reintentos:
    - SIEMPRE responde 200 rápido (ACK inmediato)
    - Procesa en background para que Evolution no reintente
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"❌ webhook: JSON inválido: {e}")
        return {"status": "ignored", "reason": "invalid_json"}

    # Log del payload (controlado Y SANITIZADO)
    _safe_log_payload("🧾 WEBHOOK: ", body)

    try:
        data_payload = body.get("data")
        if not data_payload:
            return {"status": "ignored", "reason": "no_data"}

        events = data_payload if isinstance(data_payload, list) else [data_payload]

        # ACK inmediato: dispara background y regresa
        asyncio.create_task(_background_process_events(events))
        return {"status": "accepted"}

    except Exception as e:
        logger.error(f"❌ webhook ERROR GENERAL: {e}")
        return {"status": "error_but_acked"}
