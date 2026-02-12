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
from urllib.parse import quote
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
    EVO_INSTANCE: str = "Maximo Cervantes 2"
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

    # Acumulación de mensajes rápidos
    MESSAGE_ACCUMULATION_SECONDS: float = 4.0  # Espera para acumular mensajes seguidos

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

        # 🆕 ACUMULACIÓN DE MENSAJES: Agrupa mensajes rápidos del cliente
        self.pending_messages: Dict[str, List[str]] = {}  # jid -> [msg1, msg2, ...]
        self.pending_message_tasks: Dict[str, asyncio.Task] = {}  # jid -> task
        self.last_user_message_time: Dict[str, float] = {}  # jid -> timestamp


# === 3. LIFESPAN (INICIO/CIERRE) ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Iniciando BotTractos con sistema completo...")

    bot_state = GlobalState()

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

    # Inyectar estado en app para acceso desde endpoints
    app.state.bot = bot_state

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

    # 4. AUDIO/NOTA DE VOZ (múltiples formatos de Evolution API)
    if "audioMessage" in msg_obj or "pttMessage" in msg_obj:
        return "", True

    # 5. Fallback: revisar messageType (algunas versiones de Evolution API)
    #    También verifica si hay mimetype de audio en cualquier sub-mensaje
    for k, v in msg_obj.items():
        if isinstance(v, dict):
            mimetype = v.get("mimetype", "")
            if "audio" in mimetype or "ogg" in mimetype:
                logger.info(f"🎤 Audio detectado vía mimetype en key '{k}': {mimetype}")
                return "", True

    # Log de keys no reconocidas para diagnóstico
    known_keys = {"conversation", "extendedTextMessage", "imageMessage", "audioMessage",
                  "pttMessage", "messageContextInfo", "senderKeyDistributionMessage"}
    unknown = set(msg_obj.keys()) - known_keys
    if unknown:
        logger.info(f"📋 Keys de mensaje no procesadas: {unknown}")

    return "", False


async def _ensure_inventory_loaded(bot_state: GlobalState) -> None:
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


def _is_automated_greeting(text: str) -> bool:
    """
    Detecta mensajes automáticos de WhatsApp Business o sistemas externos (n8n, etc).
    Estos mensajes NO deben silenciar al bot.
    """
    if not text:
        return False

    text_lower = text.lower()

    # Patrones de mensajes de bienvenida automáticos
    automated_patterns = [
        # WhatsApp Business greeting messages
        ("bienvenido" in text_lower and "wa.me" in text_lower),
        ("catálogo" in text_lower and "wa.me" in text_lower),
        ("catalogo" in text_lower and "wa.me" in text_lower),
        # Links de catálogo de WhatsApp
        "wa.me/c/" in text_lower,
        # Mensajes de ausencia típicos
        ("no estamos disponibles" in text_lower),
        ("fuera de horario" in text_lower),
        ("te contactaremos" in text_lower and "pronto" in text_lower),
        # Mensajes de bienvenida genéricos sin contexto
        (text_lower.startswith("hola") and "bienvenido" in text_lower and len(text) < 200),
    ]

    if any(automated_patterns):
        logger.info(f"🤖 Mensaje automático detectado (NO silencia): '{text[:80]}...'")
        return True

    return False


def _is_bot_message(bot_state: GlobalState, remote_jid: str, msg_id: str, msg_text: str) -> bool:
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
    delay = random.uniform(5.0, 8.0)
    logger.info(f"⏳ Esperando {delay:.1f}s (delay humano)...")
    await asyncio.sleep(delay)


# === 6.5 PROCESAMIENTO DE MENSAJES ACUMULADOS ===
async def _process_accumulated_messages(bot_state: GlobalState, remote_jid: str):
    """
    Procesa todos los mensajes acumulados de un usuario como uno solo.
    Se ejecuta después de MESSAGE_ACCUMULATION_SECONDS sin nuevos mensajes.
    """
    # Obtener y limpiar mensajes pendientes
    messages = bot_state.pending_messages.pop(remote_jid, [])
    bot_state.pending_message_tasks.pop(remote_jid, None)

    if not messages:
        return

    # Combinar mensajes en uno solo
    if len(messages) == 1:
        combined_message = messages[0]
    else:
        combined_message = " | ".join(messages)
        logger.info(f"📦 Mensajes acumulados ({len(messages)}): '{combined_message[:100]}...'")

    # === Verificar silenciamiento ===
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

    # === Comandos especiales ===
    if combined_message.lower() == "/silencio":
        bot_state.silenced_users[remote_jid] = True
        await send_evolution_message(bot_state, remote_jid, "Bot desactivado. Un asesor humano te atenderá en breve.")
        if settings.OWNER_PHONE:
            clean_client = remote_jid.split("@")[0]
            alerta = f"*HANDOFF ACTIVADO*\n\nEl chat con wa.me/{clean_client} ha sido pausado."
            await send_evolution_message(bot_state, settings.OWNER_PHONE, alerta)
        return

    if combined_message.lower() == "/activar":
        bot_state.silenced_users.pop(remote_jid, None)
        await send_evolution_message(bot_state, remote_jid, "Bot activado de nuevo. ¿En qué te ayudo?")
        return

    # === Refrescar inventario ===
    await _ensure_inventory_loaded(bot_state)

    store = bot_state.store
    if not store:
        logger.error("❌ MemoryStore no inicializado.")
        return

    session = await store.get(remote_jid) or {"state": "start", "context": {}}
    state = session.get("state", "start")
    context = session.get("context", {}) or {}

    # Delay humano
    await human_typing_delay()

    # === Procesar con IA ===
    try:
        result = await handle_message(combined_message, bot_state.inventory, state, context)
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
    pdf_info = result.get("pdf_info")

    # Guardar estado
    try:
        await store.upsert(
            remote_jid,
            str(result.get("new_state", state)),
            dict(result.get("context", context)),
        )
    except Exception as e:
        logger.error(f"⚠️ Error guardando memoria: {e}")

    # Verificar si hay que enviar un PDF
    if pdf_info:
        logger.info(f"📄 PDF info recibido: {pdf_info}")
        if pdf_info.get("pdf_url"):
            # Enviar texto + PDF
            logger.info(f"📤 Enviando PDF: {pdf_info.get('filename')} -> {remote_jid}")
            await send_evolution_document(
                bot_state,
                remote_jid,
                reply_text,
                pdf_info.get("pdf_url"),
                pdf_info.get("filename", "documento.pdf")
            )
        else:
            # PDF detectado pero no disponible - enviar solo texto
            logger.info(f"📄 PDF detectado pero no disponible: {pdf_info}")
            await send_evolution_message(bot_state, remote_jid, reply_text, media_urls)
    else:
        # Enviar respuesta normal (texto + fotos si las hay)
        await send_evolution_message(bot_state, remote_jid, reply_text, media_urls)

    # === FUNNEL TRACKING (V2) ===
    funnel_stage = result.get("funnel_stage", "1er Contacto")
    funnel_data = result.get("funnel_data", {})
    is_disinterest = result.get("is_disinterest", False)
    previous_stage = context.get("funnel_stage", "")

    # V2: Update Monday on any stage change, including 1er Contacto and new stages
    v2_stages = ("1er Contacto", "Intención", "Cotización", "Cita Programada", "Sin Interes")
    should_update_monday = (
        funnel_stage in v2_stages and
        funnel_stage != previous_stage
    )

    if should_update_monday:
        try:
            funnel_key = f"{remote_jid}|{funnel_stage}"
            if funnel_key not in bot_state.processed_lead_ids:
                bot_state.processed_lead_ids.add(funnel_key)

                lead_data = {
                    "telefono": remote_jid.split("@")[0],
                    "external_id": f"accumulated_{int(time.time())}",
                    "nombre": funnel_data.get("nombre") or "Lead sin nombre",
                    "interes": funnel_data.get("interes") or "",
                    "cita": funnel_data.get("cita"),
                    "pago": funnel_data.get("pago"),
                }

                stage_notes = {
                    "1er Contacto": f"📩 Primer contacto (turno {funnel_data.get('turn_count', '?')})",
                    "Intención": f"🎯 Interesado en: {funnel_data.get('interes', 'N/A')}",
                    "Cotización": f"📄 Cotización enviada: {funnel_data.get('interes', 'N/A')}",
                    "Cita Programada": f"✅ Cita programada: {funnel_data.get('cita', 'N/A')}",
                    "Sin Interes": f"🚫 Lead expresó desinterés",
                }
                note = stage_notes.get(funnel_stage)

                logger.info(f"📊 FUNNEL V2 [{funnel_stage}]: {lead_data.get('telefono')} - {lead_data.get('interes')}")
                await monday_service.create_or_update_lead(lead_data, stage=funnel_stage, add_note=note)

        except Exception as e:
            logger.error(f"❌ Error actualizando funnel en Monday: {e}")

    # Lead calificado - notificar
    if lead_info:
        try:
            lead_key = f"{remote_jid}|lead"
            if lead_key not in bot_state.processed_lead_ids:
                bot_state.processed_lead_ids.add(lead_key)
                await notify_owner(bot_state, remote_jid, combined_message, reply_text, is_lead=True)
        except Exception as e:
            logger.error(f"❌ Error procesando LEAD calificado: {e}")
    else:
        await notify_owner(bot_state, remote_jid, combined_message, reply_text, is_lead=False)


async def _schedule_accumulated_processing(bot_state: GlobalState, remote_jid: str):
    """
    Espera MESSAGE_ACCUMULATION_SECONDS y luego procesa los mensajes acumulados.
    Si llegan más mensajes, esta tarea se cancela y se crea una nueva.
    """
    try:
        await asyncio.sleep(settings.MESSAGE_ACCUMULATION_SECONDS)
        await _process_accumulated_messages(bot_state, remote_jid)
    except asyncio.CancelledError:
        # Se canceló porque llegó otro mensaje - normal
        pass
    except Exception as e:
        logger.error(f"❌ Error en procesamiento acumulado: {e}")


# === 7. TRANSCRIPCIÓN DE AUDIO ===
async def _handle_audio_transcription(bot_state: GlobalState, msg_id: str, remote_jid: str) -> str:
    """
    Descarga el audio DESENCRIPTADO desde Evolution API y lo transcribe con Whisper.
    Con retry y mejor diagnóstico de errores.
    """
    if not msg_id or not remote_jid:
        logger.warning("⚠️ msg_id o remote_jid vacío para audio")
        return ""

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
            temp_path = temp_audio.name

        client = bot_state.http_client
        if not client:
            logger.error("❌ Cliente HTTP no inicializado para audio")
            return ""

        # === PASO 1: Descargar audio de Evolution API (con retry) ===
        media_url = f"/chat/getBase64FromMediaMessage/{quote(settings.EVO_INSTANCE, safe='')}"

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

        base64_audio = None
        for _audio_attempt in range(3):
            logger.info(f"⬇️ Descargando audio (intento {_audio_attempt + 1}/3)...")
            try:
                response = await _evo_post(client, media_url, json=payload)
            except Exception as e:
                logger.error(f"❌ Error de conexión descargando audio: {e}")
                if _audio_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            if response.status_code not in [200, 201]:
                logger.error(f"❌ Evolution audio status {response.status_code}: {response.text[:200]}")
                if _audio_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            try:
                data = response.json()
            except Exception as e:
                logger.error(f"❌ Evolution audio respuesta no JSON: {e}")
                if _audio_attempt < 2:
                    await asyncio.sleep(2.0)
                continue

            # Extraer base64 de diferentes formatos de respuesta
            if isinstance(data, dict):
                base64_audio = data.get("base64") or data.get("media") or data.get("data")
                if not base64_audio:
                    logger.error(f"❌ Respuesta sin base64. Keys: {list(data.keys())}")
            elif isinstance(data, str):
                base64_audio = data
            else:
                logger.error(f"❌ Tipo de respuesta inesperado: {type(data)}")

            if base64_audio:
                # Limpiar data URI prefix si existe
                if "base64," in base64_audio:
                    base64_audio = base64_audio.split("base64,", 1)[1]
                break
            elif _audio_attempt < 2:
                await asyncio.sleep(2.0)

        if not base64_audio:
            logger.error("❌ No se pudo obtener base64 de audio después de 3 intentos")
            return ""

        # Decodificar y guardar
        try:
            audio_bytes = base64.b64decode(base64_audio)
        except Exception as e:
            logger.error(f"❌ Error decodificando base64 audio: {e}")
            return ""

        with open(temp_path, "wb") as f:
            f.write(audio_bytes)

        logger.info(f"✅ Audio descargado: {len(audio_bytes)} bytes")

        # Verificar que el archivo no esté vacío o corrupto
        if len(audio_bytes) < 100:
            logger.error(f"❌ Audio demasiado pequeño ({len(audio_bytes)} bytes), posiblemente corrupto")
            return ""

        # === PASO 2: Transcribir con Whisper ===
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
            except Exception:
                pass


# === 8. ENVÍO DE MENSAJES (CON RASTREO) ===
async def send_evolution_message(bot_state: GlobalState, number_or_jid: str, text: str, media_urls: Optional[List[str]] = None):
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
            fotos_enviadas = 0
            for i, media_url in enumerate(media_urls):
                url = f"/message/sendMedia/{quote(settings.EVO_INSTANCE, safe='')}"

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

                # Retry individual photo sends (up to 2 retries)
                sent = False
                for _photo_attempt in range(3):
                    response = await _evo_post(client, url, json=payload)
                    if response.status_code < 400:
                        sent = True
                        fotos_enviadas += 1
                        logger.info(f"✅ Enviada foto {i+1}/{total_fotos} a {clean_number}")
                        try:
                            resp_data = response.json()
                            msg_id = resp_data.get("key", {}).get("id")
                            if msg_id:
                                bot_state.bot_sent_message_ids.add(msg_id)
                        except Exception:
                            pass
                        break
                    else:
                        if _photo_attempt < 2:
                            logger.warning(f"⚠️ Retry foto {i+1} intento {_photo_attempt+1}: {response.status_code}")
                            await asyncio.sleep(1.5)
                        else:
                            logger.error(f"❌ Error foto {i+1} después de 3 intentos: {response.text[:200]}")

            # If no photos were sent but we had text with photo promise, send text anyway
            if fotos_enviadas == 0 and text:
                logger.warning(f"⚠️ Ninguna foto enviada, enviando solo texto")
                url_text = f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}"
                payload_text = {"number": clean_number, "text": text}
                await _evo_post(client, url_text, json=payload_text)

        else:
            url = f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}"
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


async def send_evolution_document(bot_state: GlobalState, number_or_jid: str, text: str, pdf_url: str, filename: str):
    """
    Envía primero un mensaje de texto y luego un PDF como documento.
    El texto se envía antes del PDF para dar contexto al usuario.
    """
    clean_number = _clean_phone_or_jid(number_or_jid)
    if not clean_number:
        logger.error(f"❌ No se pudo limpiar número/jid: {number_or_jid}")
        return

    client = bot_state.http_client
    if not client:
        logger.error("❌ Cliente HTTP no inicializado (lifespan).")
        return

    try:
        # 1. Enviar texto primero
        if text:
            url_text = f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}"
            payload_text = {"number": clean_number, "text": text}
            response = await _evo_post(client, url_text, json=payload_text)

            if response.status_code >= 400:
                logger.error(f"⚠️ Error enviando texto antes de PDF: {response.text}")
            else:
                logger.info(f"✅ Texto enviado antes de PDF a {clean_number}")
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get("key", {}).get("id")
                    if msg_id:
                        bot_state.bot_sent_message_ids.add(msg_id)
                except Exception:
                    pass

            # Pequeña espera para que WhatsApp ordene los mensajes
            await asyncio.sleep(1.2)

        # 2. Enviar PDF como documento (con retry)
        url_media = f"/message/sendMedia/{quote(settings.EVO_INSTANCE, safe='')}"
        payload_pdf = {
            "number": clean_number,
            "mediatype": "document",
            "mimetype": "application/pdf",
            "media": pdf_url,
            "fileName": filename,
            "caption": ""
        }

        pdf_sent = False
        for _pdf_attempt in range(3):
            response = await _evo_post(client, url_media, json=payload_pdf)
            if response.status_code < 400:
                pdf_sent = True
                logger.info(f"✅ PDF enviado a {clean_number}: {filename}")
                try:
                    resp_data = response.json()
                    msg_id = resp_data.get("key", {}).get("id")
                    if msg_id:
                        bot_state.bot_sent_message_ids.add(msg_id)
                except Exception:
                    pass
                break
            else:
                if _pdf_attempt < 2:
                    logger.warning(f"⚠️ Retry PDF intento {_pdf_attempt+1}: {response.status_code}")
                    await asyncio.sleep(2.0)
                else:
                    logger.error(f"❌ Error enviando PDF después de 3 intentos: {response.text[:200]}")

        if not pdf_sent:
            # Fallback: inform the user the PDF couldn't be sent
            fallback_text = "No pude enviar el PDF en este momento. Un asesor te lo comparte."
            url_fallback = f"/message/sendText/{quote(settings.EVO_INSTANCE, safe='')}"
            await _evo_post(client, url_fallback, json={"number": clean_number, "text": fallback_text})

    except httpx.RequestError as e:
        logger.error(f"❌ Error de conexión enviando PDF: {e}")
    except Exception as e:
        logger.error(f"❌ Error inesperado enviando PDF: {e}")


# === 9. ALERTAS AL DUEÑO ===
async def notify_owner(bot_state: GlobalState, user_number_or_jid: str, user_message: str, bot_reply: str, is_lead: bool = False):
    if not settings.OWNER_PHONE:
        return

    clean_client = _clean_phone_or_jid(user_number_or_jid)

    if is_lead:
        alert_text = (
            "*NUEVO LEAD EN MONDAY*\n\n"
            f"Cliente: wa.me/{clean_client}\n"
            "El bot cerró una cita. Revisa el tablero."
        )
        await send_evolution_message(bot_state, settings.OWNER_PHONE, alert_text)
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
    await send_evolution_message(bot_state, settings.OWNER_PHONE, alert_text)


# === 10. PROCESADOR CENTRAL ===
async def process_single_event(bot_state: GlobalState, data: Dict[str, Any]):
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
    # Si el mensaje sale del WhatsApp del negocio (from_me=true)
    # y NO fue enviado por el bot → PODRÍA ser un HUMANO ASESOR
    if from_me:
        msg_obj = data.get("message", {}) or {}
        msg_text, _ = _extract_user_message(msg_obj)
        msg_text = msg_text.strip()

        # 1. Verificar si este mensaje fue enviado por el bot
        if _is_bot_message(bot_state, remote_jid, msg_id, msg_text):
            logger.debug(f"✓ Confirmado mensaje del bot, ignorando")
            return

        # 2. Verificar si es un mensaje automático (WhatsApp Business greeting, n8n, etc)
        #    Estos NO deben silenciar al bot
        if _is_automated_greeting(msg_text):
            logger.info(f"✓ Mensaje automático ignorado (bot sigue activo)")
            return

        # 3. Si NO es del bot Y NO es automático → Es un HUMANO → SILENCIAR
        logger.info(f"🤐 HUMANO DETECTADO en {remote_jid} - silenciando bot por {settings.AUTO_REACTIVATE_MINUTES} min")
        bot_state.silenced_users[remote_jid] = time.time() + (settings.AUTO_REACTIVATE_MINUTES * 60)
        return

    # === EXTRACCIÓN DE MENSAJE (TEXTO O AUDIO) ===
    msg_obj = data.get("message", {}) or {}
    user_message, is_audio = _extract_user_message(msg_obj)
    user_message = user_message.strip()

    # Si NO hay texto y es audio, transcribir
    if not user_message and is_audio:
        logger.info(f"🎤 Audio detectado, procesando...")
        user_message = await _handle_audio_transcription(bot_state, msg_id, remote_jid)

        if not user_message:
            await send_evolution_message(
                bot_state, remote_jid,
                "Tuve un problema escuchando el audio. ¿Me lo puedes escribir o mandar de nuevo?"
            )
            return

        logger.info(f"✅ Transcripción exitosa, procesando como texto...")

    if not user_message:
        return

    # === ACUMULACIÓN DE MENSAJES RÁPIDOS ===
    # En lugar de procesar inmediatamente, acumulamos y esperamos
    # para ver si el cliente envía más mensajes seguidos

    # Agregar mensaje a la lista pendiente
    if remote_jid not in bot_state.pending_messages:
        bot_state.pending_messages[remote_jid] = []
    bot_state.pending_messages[remote_jid].append(user_message)

    logger.info(f"📥 Mensaje acumulado ({len(bot_state.pending_messages[remote_jid])} pendientes): '{user_message[:50]}...'")

    # Cancelar tarea anterior si existe (reinicia el timer)
    if remote_jid in bot_state.pending_message_tasks:
        old_task = bot_state.pending_message_tasks[remote_jid]
        if not old_task.done():
            old_task.cancel()
            logger.debug(f"⏱️ Timer reiniciado para {remote_jid}")

    # Programar nuevo procesamiento después de MESSAGE_ACCUMULATION_SECONDS
    task = asyncio.create_task(_schedule_accumulated_processing(bot_state, remote_jid))
    bot_state.pending_message_tasks[remote_jid] = task


# === 11. ENDPOINTS ===
@app.get("/health")
async def health(request: Request):
    """Endpoint de salud con métricas del sistema."""
    bot_state: GlobalState = request.app.state.bot
    return {
        "status": "ok",
        "instance": settings.EVO_INSTANCE,
        "inventory_count": len(getattr(bot_state.inventory, "items", []) or []),
        "silenced_chats": len(bot_state.silenced_users),
        "processed_msgs_cache": len(bot_state.processed_message_ids),
        "processed_leads_cache": len(bot_state.processed_lead_ids),
        "bot_messages_tracked": len(bot_state.bot_sent_message_ids),
        "pending_message_queues": len(bot_state.pending_messages),
        "handoff_enabled": len(TEAM_NUMBERS_LIST) > 0,
        "auto_reactivate_minutes": settings.AUTO_REACTIVATE_MINUTES,
        "message_accumulation_seconds": settings.MESSAGE_ACCUMULATION_SECONDS,
    }


async def _background_process_events(bot_state: GlobalState, events: List[Dict[str, Any]]):
    """Procesa eventos en background para ACK inmediato al webhook."""
    for event in events:
        try:
            await process_single_event(bot_state, event)
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
        bot_state: GlobalState = request.app.state.bot
        asyncio.create_task(_background_process_events(bot_state, events))
        return {"status": "accepted"}

    except Exception as e:
        logger.error(f"❌ webhook ERROR GENERAL: {e}")
        return {"status": "error_but_acked"}

