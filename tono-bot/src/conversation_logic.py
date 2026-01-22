import os
import re
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import pytz
from openai import OpenAI

logger = logging.getLogger(__name__)

# === CONFIGURACIÓN DE IA ===
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_NAME = "gpt-4o-mini"


# === HELPER DE TIEMPO ===
def get_mexico_time() -> Tuple[datetime, str]:
    """Devuelve la fecha y hora actual en CDMX (datetime y string legible)."""
    try:
        tz = pytz.timezone("America/Mexico_City")
        now = datetime.now(tz)
        return now, now.strftime("%A %I:%M %p")
    except Exception as e:
        logger.error(f"Error timezone: {e}")
        now = datetime.now()
        return now, now.strftime("%A %I:%M %p")


# === PROMPT (DOBLES LLAVES PARA NO ROMPER .format) ===
SYSTEM_PROMPT = """
Eres "Adrian", Asesor Comercial de 'Tractos y Max'.

OBJETIVO: Vender camiones, resolver dudas y agendar visitas.

DATOS CLAVE:
- Ubicación: Av. de los Camioneros 123 (Portón Azul).
- Horario: Lunes a Viernes 9:00 AM a 6:00 PM. Sábados 9:00 AM a 2:00 PM.
- MOMENTO ACTUAL: {current_time_str}
- CLIENTE DETECTADO: {user_name_context}

REGLAS OBLIGATORIAS:

1) NOMBRE (NATURAL, NO INSISTENTE):
   - Si "CLIENTE DETECTADO" tiene un nombre, úsalo de vez en cuando (por ejemplo: "Perfecto, {user_name_context}").
   - Si NO lo tienes, NO lo pidas al inicio. Gana confianza.
   - Pídelo SOLO al final cuando el cliente muestre interés real (pide precio/fotos) o quiera cita.
   - Frase casual: "Por cierto, ¿con quién tengo el gusto?" o "¿A nombre de quién registro la visita?".

2) FOTOS (CERO CONTRADICCIONES):
   - ASUME QUE SÍ TIENES FOTOS. El sistema las adjuntará automáticamente.
   - PROHIBIDO DECIR: "No puedo enviar fotos", "No tengo imágenes", "Soy una IA", "Soy un modelo".
   - Si piden fotos, di: "Claro, aquí tienes." o "Mira esta unidad." (y deja que el sistema pegue la foto).

3) RELOJ:
   - Si es FUERA de horario, di que la oficina está cerrada y ofrece agendar para mañana.

4) MONDAY (CANDADO DE SEGURIDAD):
   - SOLO genera el JSON oculto si tienes:
     A) NOMBRE REAL (No uses "Cliente", "Amigo", "Desconocido").
     B) INTERÉS (modelo).
     C) CITA o INTENCIÓN CLARA.
   - Si falta el nombre, PREGÚNTALO antes de generar el JSON.
   - Formato EXACTO (usa dobles llaves):
   ```json
   {{
     "lead_event": {{
       "nombre": "Juan Perez",
       "interes": "Foton Tunland E5 2024",
       "cita": "Viernes 10am",
       "pago": "Contado"
     }}
   }}
   ```

NO REPETIR: No repitas saludos ni direcciones si ya las diste hace poco.
INVENTARIO: Vende solo lo que ves en la lista.
MODO GPS: Si piden ubicación, dales la dirección exacta y referencia visual (sin fotos).

ESTILO: Amable, directo y profesional. Máximo 3 oraciones.
""".strip()


def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    """Devuelve el primer valor no vacío encontrado en item para las llaves dadas."""
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def _build_inventory_text(inventory_service) -> str:
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return "Inventario no disponible."

    lines: List[str] = []
    for item in items:
        marca = _safe_get(item, ["Marca", "marca"], default="(sin marca)")
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"], default="(sin modelo)")
        anio = _safe_get(item, ["Anio", "Año", "anio"], default="")
        precio = _safe_get(item, ["Precio", "precio"], default="N/D")
        status = _safe_get(item, ["status", "disponible"], default="Disponible")
        desc = _safe_get(item, ["descripcion_corta", "segmento"], default="")

        info = f"- {marca} {modelo} {anio}: ${precio} ({status})".strip()
        if desc:
            info += f" [{desc}]"
        lines.append(info)

    return "\n".join(lines)


def _extract_photos_from_item(item: Dict[str, Any]) -> List[str]:
    raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes", "fotos"])
    if not raw:
        return []
    return [u.strip() for u in raw.split("|") if u.strip().startswith("http")]


def _extract_name_from_text(text: str) -> Optional[str]:
    """Extrae nombre probable del cliente (heurística simple, conservadora)."""
    t = (text or "").strip()
    if not t:
        return None

    patterns = [
        r"\bme llamo\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bsoy\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bmi nombre es\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
    ]

    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            bad = {"aqui", "aquí", "nadie", "yo", "el", "ella", "amigo", "desconocido", "cliente"}
            if name.lower() in bad:
                return None
            return " ".join(w.capitalize() for w in name.split())

    # Conservador: NO aceptar un solo token sin frase (evita falsos positivos)
    return None


def _pick_media_urls(user_message: str, reply: str, inventory_service) -> List[str]:
    msg = (user_message or "").lower()

    # 1) FILTRO GPS: si piden ubicación, PROHIBIDO mandar fotos
    gps_keywords = [
        "ubicacion", "ubicación", "donde estan", "dónde están",
        "direccion", "dirección", "mapa", "donde se ubican"
    ]
    if any(k in msg for k in gps_keywords):
        return []

    items = getattr(inventory_service, "items", None) or []
    if not items:
        return []

    def norm(text: str) -> str:
        return (
            (text or "")
            .lower()
            .replace("miller", "miler")
            .replace("vanesa", "toano")
            .replace("la e5", "tunland e5")
        )

    msg_norm = norm(user_message)
    rep_norm = norm(reply)

    # 2) Gatekeeper: SOLO mandamos fotos si el usuario pide ver fotos explícitamente
    photo_keywords = [
        "foto", "fotos", "imagen", "imagenes", "imágenes",
        "ver fotos", "ver imágenes", "ver la foto", "ver las fotos",
        "enseñame", "enséñame", "muestrame", "muéstrame",
        "mandame fotos", "mándame fotos", "quiero ver",
    ]
    if not any(k in msg_norm for k in photo_keywords):
        return []

    # 3) Si pidió fotos: match por usuario primero, luego por contexto del bot
    for item in items:
        urls = _extract_photos_from_item(item)
        if not urls:
            continue

        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).lower().strip()
        if not modelo:
            continue

        parts = modelo.split()

        match_user = any(
            part in msg_norm
            for part in parts
            if len(part) >= 3 and part not in ["foton", "camion", "camión"]
        )
        match_bot = any(
            part in rep_norm
            for part in parts
            if len(part) >= 3 and part not in ["foton", "camion", "camión"]
        )

        if match_user or match_bot:
            return urls

    return []


def _sanitize_reply_if_photos_attached(reply: str, media_urls: List[str]) -> str:
    """Evita el 'no puedo enviar fotos' cuando el sistema sí adjunta."""
    if not media_urls:
        return reply

    bad_phrases = [
        r"no\s+puedo\s+enviar\s+fotos",
        r"no\s+puedo\s+mandar\s+fotos",
        r"no\s+tengo\s+fotos",
        r"no\s+puedo\s+enviar\s+im[aá]genes",
        r"no\s+puedo\s+mandar\s+im[aá]genes",
        r"soy\s+una\s+ia",
        r"soy\s+un\s+modelo",
    ]

    cleaned = reply or ""
    combined = "|".join(bad_phrases)
    if re.search(combined, cleaned, flags=re.IGNORECASE):
        cleaned = re.sub(combined, "Claro, aquí tienes.", cleaned, flags=re.IGNORECASE)

    return cleaned


def _lead_is_valid(lead: Dict[str, Any]) -> bool:
    """CANDADO DURO: valida que el lead tenga datos reales."""
    if not isinstance(lead, dict):
        return False

    nombre = str(lead.get("nombre", "")).strip()
    interes = str(lead.get("interes", "")).strip()
    cita = str(lead.get("cita", "")).strip()

    # Nombre
    if not nombre or len(nombre) < 3:
        return False

    placeholders = {
        "cliente nuevo", "desconocido", "amigo", "cliente", "nuevo lead", "usuario", "no proporcionado"
    }
    if nombre.lower() in placeholders:
        return False

    if not re.search(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü]", nombre):
        return False

    # Interés y cita
    if not interes or len(interes) < 2:
        return False
    if not cita or len(cita) < 2:
        return False

    return True


def handle_message(
    user_message: str,
    inventory_service,
    state: str,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    user_message = user_message or ""
    context = context or {}
    history = (context.get("history") or "").strip()

    # === MODO SILENCIO ===
    if user_message.strip().lower() == "/silencio":
        new_history = (history + f"\nC: {user_message}\nA: Perfecto. Modo silencio activado.").strip()
        return {
            "reply": "Perfecto. Modo silencio activado.",
            "new_state": "silent",
            "context": {"history": new_history[-4000:]},
            "media_urls": [],
            "lead_info": None,
        }

    if state == "silent":
        return {
            "reply": "",
            "new_state": "silent",
            "context": context,
            "media_urls": [],
            "lead_info": None,
        }

    # === CAPTURA DE NOMBRE (GUARDAR EN CONTEXTO) ===
    saved_name = (context.get("user_name") or "").strip()
    extracted = _extract_name_from_text(user_message)
    if extracted:
        saved_name = extracted

    # === HORA REAL ===
    _, current_time_str = get_mexico_time()

    formatted_system_prompt = SYSTEM_PROMPT.format(
        current_time_str=current_time_str,
        user_name_context=saved_name if saved_name else "(Aún no dice su nombre)",
    )

    inventory_text = _build_inventory_text(inventory_service)

    context_block = (
        f"MOMENTO ACTUAL: {current_time_str}\n"
        f"CLIENTE DETECTADO: {saved_name or '(Desconocido)'}\n"
        f"INVENTARIO DISPONIBLE:\n{inventory_text}\n\n"
        f"HISTORIAL DE CHAT:\n{history[-3000:]}"
    )

    messages = [
        {"role": "system", "content": formatted_system_prompt},
        {"role": "user", "content": context_block},
        {"role": "user", "content": user_message},
    ]

    lead_info: Optional[Dict[str, Any]] = None
    reply_clean = "Hubo un error técnico."

    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.3,
            max_tokens=350,
        )

        raw_reply = resp.choices[0].message.content or ""
        reply_clean = raw_reply

        # === EXTRAER JSON DE LEAD (SI EXISTE) ===
        json_match = re.search(r"```json\s*({.*?})\s*```", raw_reply, re.DOTALL)
        if json_match:
            try:
                payload = json.loads(json_match.group(1))
                candidate = payload.get("lead_event") if isinstance(payload, dict) else None

                if isinstance(candidate, dict):
                    # Inyectar nombre guardado si el modelo dejó placeholder o vacío
                    nombre_candidato = str(candidate.get("nombre", "")).strip()
                    if (not nombre_candidato or nombre_candidato.lower() in ["cliente", "desconocido"]) and saved_name:
                        candidate["nombre"] = saved_name

                    if _lead_is_valid(candidate):
                        lead_info = candidate
                    else:
                        logger.warning(f"Lead descartado por incompleto: {candidate}")

                # Siempre esconder el JSON del usuario final
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()
            except Exception:
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()

    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        reply_clean = "Dame un momento, estoy consultando sistema..."

    # Limpieza de prefijos tipo "Adrian:"
    reply_clean = re.sub(
        r"^(Adrian|Asesor|Bot)\s*:\s*",
        "",
        reply_clean.strip(),
        flags=re.IGNORECASE,
    ).strip()

    # Adjuntar fotos (si aplica)
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)

    # Sanitizar contradicciones si sí adjuntamos fotos
    reply_clean = _sanitize_reply_if_photos_attached(reply_clean, media_urls)

    new_history = (history + f"\nC: {user_message}\nA: {reply_clean}").strip()

    new_context = {
        "history": new_history[-4000:],
        "user_name": saved_name,
    }

    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": new_context,
        "media_urls": media_urls,
        "lead_info": lead_info,
    }
