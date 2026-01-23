import os
import re
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import pytz
from openai import OpenAI

logger = logging.getLogger(__name__)

# ============================================================
#  CONFIG
# ============================================================
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


# ============================================================
#  TIME (CDMX)
# ============================================================
def get_mexico_time() -> Tuple[datetime, str]:
    """Returns current datetime in Mexico City timezone and a readable string."""
    try:
        tz = pytz.timezone("America/Mexico_City")
        now = datetime.now(tz)
        return now, now.strftime("%A %I:%M %p")
    except Exception as e:
        logger.error(f"Timezone error: {e}")
        now = datetime.now()
        return now, now.strftime("%A %I:%M %p")


# ============================================================
#  PROMPT (IMPORTANT: JSON example uses DOUBLE BRACES {{ }})
# ============================================================
SYSTEM_PROMPT = """
Eres "Adrian Jímenez", Asesor humano de alto nivel comercial de 'Tractos y Max'.

OBJETIVO: Vender vehículos comerciales, generar confianza, resolver dudas y agendar visitas a la agencia. Gestionar la interacción con empatía, claridad y control, logrando que el cliente se sienta bien atendido sin caer en complacencia. Dar contexto y confianza sobre quién es Tractos y Max. Validar el interés del cliente con una sola pregunta humana y breve. Confirmar de forma mínima que entendiste lo que busca el cliente

DATOS CLAVE:
- Ubicación: Tlalnepantla, Edo Mex.
- Horario: Lunes a Viernes 9:00 AM a 6:00 PM. Sábados 9:00 AM a 2:00 PM.
- MOMENTO ACTUAL: {current_time_str}
- CLIENTE DETECTADO: {user_name_context}
-Conocimiento del negocio: Tractos y Max se dedica a la comercialización de vehículos comerciales nuevos y seminuevos a precio de oportunidad.

REGLAS OBLIGATORIAS:

1) BIENVENIDA Y NOMBRE:
   - Si es el PRIMER mensaje y sabes qué vehículo le interesa, di: "Hola, veo que te interesa la [Modelo]. Soy Adrian Jimenez, ¿con quién tengo el gusto?".
   - Si el cliente solo dice "Hola" o no sabes su interés, saluda y ofrece ayuda SIN pedir el nombre aún.
   - Si ya tienes el nombre ({user_name_context}), úsalo para personalizar ("Hola Juan...").

2) TU NOMBRE vs SU NOMBRE (FLUIDEZ):
   - Si el cliente pregunta "¿Cómo te llamas?", responde primero: "Soy Adrian..."
   - Después, de forma casual, pide el suyo si no lo tienes: "¿Con quién tengo el gusto?"

3) NOMBRE (NATURAL, NO INSISTENTE):
   - Si ya tienes nombre en "CLIENTE DETECTADO", úsalo.
   - Si no lo tienes, NO lo pidas al inicio.
   - Pídelo SOLO cuando el cliente muestre interés real (precio/fotos/crédito) o al cerrar cita.
   - Frases casuales: "Por cierto, ¿con quién tengo el gusto?" / "¿A nombre de quién registro la visita?"

4) POLÍTICA DE MARCA (GARANTÍA):
   - En tu PRIMERA respuesta donde des información técnica o precios, debes mencionar casualmente (sin que suene a robot legal):
     "Te comento que todas nuestras unidades son 100% nuevas, con garantía de fábrica y facturadas directo por distribuidor FOTON."
   - Hazlo fluir con la conversación, no lo digas como una interrupción brusca.   

5) FOTOS (CERO CONTRADICCIONES):
   - Asume que SÍ hay fotos. El sistema las adjuntará automáticamente.
   - Prohibido decir: "No puedo enviar fotos", "No tengo imágenes", "Soy una IA".
   - Si piden fotos: "Claro, aquí tienes." o "Mira esta unidad."

6) RELOJ:
   - Si es FUERA de horario, di que la oficina está cerrada y ofrece agendar para mañana.
   
7) **MODO GPS (HANDOFF):**
   - Si piden ubicación, envía este enlace EXACTO: [https://maps.app.goo.gl/v9KigGY3QVAxqwV17]
   - Y aclara: "Para recibirte personalmente, es necesario agendar una cita previa. ¿Qué día podrías venir?" (No des la dirección escrita, fuerza la cita).
   

8) MONDAY (NO SPAM, PERO NO FALLAR):
   - SOLO registra lead si hay: NOMBRE REAL + INTERÉS (modelo) + CITA/INTENCIÓN clara.
   - Si falta el nombre, pídelo antes de cerrar la cita.
   - Si ya hay cita confirmada, genera el JSON oculto al final (formato EXACTO):
   ```json
   {{
     "lead_event": {{
       "nombre": "Juan Perez",
       "interes": "Foton Miler 45T RS 2024",
       "cita": "Mañana 4:30 PM",
       "pago": "Crédito"
     }}
   }}
   ```

NO REPETIR: No repitas saludos ni direcciones si ya las diste hace poco. Nunca presiones, interrogues ni repitas la misma pregunta innecesariamente.
INVENTARIO: Vende solo lo que ves en la lista.
MODO GPS: Si piden ubicación, dales la dirección exacta y una referencia visual (sin fotos).
ASUNCIÓN: Asume siempre que es una mensaje entrante.
LENGUAJE: Usa frases cortas, habladas y naturales. Evita lenguaje corporativo, de folleto o de call center.
Conversas; no sigas un formulario.
Evita repetir la misma estructura más de dos veces.
Satisfacción sin complacencia.
El cliente debe sentirse escuchado y bien atendido,
pero no intentes resolver todo, convencer ni “quedar bien”.
Marca límites con naturalidad y ofrece un siguiente paso claro.

ESTILO: Amable, directo y profesional. Máximo 3 oraciones.
""".strip()


# ============================================================
#  INVENTORY HELPERS
# ============================================================
def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    """Return first non-empty string for given keys."""
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


# ============================================================
#  NAME / PAYMENT / APPOINTMENT EXTRACTION
# ============================================================
def _extract_name_from_text(text: str) -> Optional[str]:
    """Extract probable customer name (conservative)."""
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
            bad = {"aqui", "aquí", "nadie", "yo", "el", "ella", "amigo", "desconocido", "cliente", "usuario"}
            if name.lower() in bad:
                return None
            return " ".join(w.capitalize() for w in name.split())

    return None


def _extract_payment_from_text(text: str) -> Optional[str]:
    msg = (text or "").lower()
    if any(k in msg for k in ["contado", "cash", "de contado"]):
        return "Contado"
    if any(k in msg for k in ["crédito", "credito", "financiamiento", "financiación"]):
        return "Crédito"
    return None


def _normalize_spanish(text: str) -> str:
    return (
        (text or "")
        .lower()
        .replace("miller", "miler")
        .replace("vanesa", "toano")
        .replace("la e5", "tunland e5")
    )


def _extract_interest_from_messages(user_message: str, reply: str, inventory_service) -> Optional[str]:
    """Infer model interest by matching inventory model tokens in user message or bot reply."""
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return None

    msg_norm = _normalize_spanish(user_message)
    rep_norm = _normalize_spanish(reply)

    best: Optional[str] = None
    best_score = 0

    for item in items:
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
        if not modelo:
            continue
        modelo_norm = _normalize_spanish(modelo)

        tokens = [t for t in modelo_norm.split() if len(t) >= 3 and t not in {"foton", "camion", "camión"}]
        if not tokens:
            continue

        score = 0
        for tok in tokens:
            if tok in msg_norm:
                score += 2
            if tok in rep_norm:
                score += 1

        if score > best_score:
            best_score = score
            best = modelo

    if best_score >= 2:
        return best
    return None


def _extract_appointment_from_text(text: str) -> Optional[str]:
    """Basic Spanish appointment extractor for day/time."""
    t = (text or "").strip().lower()
    if not t:
        return None

    day = None
    if "mañana" in t:
        day = "Mañana"
    else:
        days = ["lunes", "martes", "miércoles", "miercoles", "jueves", "viernes", "sábado", "sabado", "domingo"]
        for d in days:
            if d in t:
                day = d.capitalize().replace("Miercoles", "Miércoles").replace("Sabado", "Sábado")
                break

    time_str = None

    m = re.search(r"\b(\d{1,2})\s*y\s*media\b", t)
    if m:
        h = int(m.group(1))
        time_str = f"{h}:30"

    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", t)
        if m:
            h = int(m.group(1))
            mm = int(m.group(2))
            if 0 <= h <= 23 and 0 <= mm <= 59:
                time_str = f"{h}:{mm:02d}"

    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
        if m:
            h = int(m.group(1))
            mer = m.group(2)
            if 1 <= h <= 12:
                hh = h % 12
                if mer == "pm":
                    hh += 12
                time_str = f"{hh}:00"

    if not time_str:
        if "en la tarde" in t or "por la tarde" in t:
            time_str = "(tarde)"
        elif "en la mañana" in t or "por la mañana" in t:
            time_str = "(mañana)"
        elif "en la noche" in t or "por la noche" in t:
            time_str = "(noche)"

    def _pretty_time_24_to_12(h24: int, mm: str) -> str:
        suffix = "AM"
        if h24 == 0:
            hh12 = 12
            suffix = "AM"
        elif 1 <= h24 <= 11:
            hh12 = h24
            suffix = "AM"
        elif h24 == 12:
            hh12 = 12
            suffix = "PM"
        else:
            hh12 = h24 - 12
            suffix = "PM"
        return f"{hh12}:{mm} {suffix}"

    if day and time_str:
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            h24 = int(time_str.split(":")[0])
            mm = time_str.split(":")[1]
            return f"{day} {_pretty_time_24_to_12(h24, mm)}"
        return f"{day} {time_str}"

    if day and not time_str:
        return day

    if time_str and not day:
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            h24 = int(time_str.split(":")[0])
            mm = time_str.split(":")[1]
            return _pretty_time_24_to_12(h24, mm)
        return time_str

    return None


def _message_confirms_appointment(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    confirmations = [
        "vale", "ok", "okey", "listo", "perfecto", "nos vemos", "ahí nos vemos",
        "mañana nos vemos", "de acuerdo", "confirmo",
    ]
    return any(c in t for c in confirmations)


# ============================================================
#  PHOTOS LOGIC
# ============================================================
def _pick_media_urls(user_message: str, reply: str, inventory_service) -> List[str]:
    msg = _normalize_spanish(user_message)

    gps_keywords = [
        "ubicacion", "ubicación", "donde estan", "dónde están",
        "direccion", "dirección", "mapa", "donde se ubican"
    ]
    if any(k in msg for k in gps_keywords):
        return []

    items = getattr(inventory_service, "items", None) or []
    if not items:
        return []

    rep_norm = _normalize_spanish(reply)

    photo_keywords = [
        "foto", "fotos", "imagen", "imagenes", "imágenes",
        "ver fotos", "ver imágenes", "ver la foto", "ver las fotos",
        "enseñame", "enséñame", "muestrame", "muéstrame",
        "mandame fotos", "mándame fotos", "quiero ver"
    ]
    if not any(k in msg for k in photo_keywords):
        return []

    for item in items:
        urls = _extract_photos_from_item(item)
        if not urls:
            continue

        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).lower().strip()
        if not modelo:
            continue

        parts = modelo.split()

        match_user = any(
            part in msg for part in parts
            if len(part) >= 3 and part not in ["foton", "camion", "camión"]
        )
        match_bot = any(
            part in rep_norm for part in parts
            if len(part) >= 3 and part not in ["foton", "camion", "camión"]
        )

        if match_user or match_bot:
            return urls

    return []


def _sanitize_reply_if_photos_attached(reply: str, media_urls: List[str]) -> str:
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
    for p in bad_phrases:
        cleaned = re.sub(p, "Claro, aquí tienes.", cleaned, flags=re.IGNORECASE)
    return cleaned


# ============================================================
#  MONDAY VALIDATION (HARD GATE)
# ============================================================
def _lead_is_valid(lead: Dict[str, Any]) -> bool:
    if not isinstance(lead, dict):
        return False

    nombre = str(lead.get("nombre", "")).strip()
    interes = str(lead.get("interes", "")).strip()
    cita = str(lead.get("cita", "")).strip()

    if not nombre or len(nombre) < 3:
        return False

    placeholders = {"cliente nuevo", "desconocido", "amigo", "cliente", "nuevo lead", "usuario", "no proporcionado"}
    if nombre.lower() in placeholders:
        return False

    if not re.search(r"[a-zA-ZÁÉÍÓÚÑÜáéíóúñü]", nombre):
        return False

    if not interes or len(interes) < 2:
        return False

    if not cita or len(cita) < 2:
        return False

    return True


# ============================================================
#  MAIN ENTRY
# ============================================================
def handle_message(
    user_message: str,
    inventory_service,
    state: str,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    user_message = user_message or ""
    context = context or {}
    history = (context.get("history") or "").strip()

    # Silence mode
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

    # Persistent context
    saved_name = (context.get("user_name") or "").strip()
    last_interest = (context.get("last_interest") or "").strip()
    last_appointment = (context.get("last_appointment") or "").strip()
    last_payment = (context.get("last_payment") or "").strip()

    # Extract from user input
    extracted_name = _extract_name_from_text(user_message)
    if extracted_name:
        saved_name = extracted_name

    extracted_payment = _extract_payment_from_text(user_message)
    if extracted_payment:
        last_payment = extracted_payment

    extracted_appt = _extract_appointment_from_text(user_message)
    if extracted_appt:
        last_appointment = extracted_appt

    # Time
    _, current_time_str = get_mexico_time()

    formatted_system_prompt = SYSTEM_PROMPT.format(
        current_time_str=current_time_str,
        user_name_context=saved_name if saved_name else "(Aún no dice su nombre)",
    )

    inventory_text = _build_inventory_text(inventory_service)

    context_block = (
        f"MOMENTO ACTUAL: {current_time_str}\n"
        f"CLIENTE DETECTADO: {saved_name or '(Desconocido)'}\n"
        f"INTERÉS DETECTADO: {last_interest or '(Sin modelo)'}\n"
        f"CITA DETECTADA: {last_appointment or '(Sin cita)'}\n"
        f"PAGO DETECTADO: {last_payment or '(Por definir)'}\n"
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

        # Update interest using user+bot text
        inferred_interest = _extract_interest_from_messages(user_message, raw_reply, inventory_service)
        if inferred_interest:
            last_interest = inferred_interest

        # Extract optional JSON from the model
        json_match = re.search(r"```json\s*({.*?})\s*```", raw_reply, re.DOTALL)
        if json_match:
            try:
                payload = json.loads(json_match.group(1))
                candidate = payload.get("lead_event") if isinstance(payload, dict) else None

                if isinstance(candidate, dict):
                    # Inject what we already know
                    if not str(candidate.get("nombre", "")).strip() and saved_name:
                        candidate["nombre"] = saved_name
                    if not str(candidate.get("interes", "")).strip() and last_interest:
                        candidate["interes"] = last_interest
                    if not str(candidate.get("cita", "")).strip() and last_appointment:
                        candidate["cita"] = last_appointment
                    if not str(candidate.get("pago", "")).strip() and last_payment:
                        candidate["pago"] = last_payment

                    if _lead_is_valid(candidate):
                        lead_info = candidate
                    else:
                        logger.warning(f"Lead JSON discarded (incomplete): {candidate}")

                # Hide JSON from user-facing message
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()
            except Exception:
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()

    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply_clean = "Dame un momento, estoy consultando sistema..."

    # Clean prefixes
    reply_clean = re.sub(
        r"^(Adrian|Asesor|Bot)\s*:\s*",
        "",
        reply_clean.strip(),
        flags=re.IGNORECASE,
    ).strip()

    # Photos (if requested)
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)
    reply_clean = _sanitize_reply_if_photos_attached(reply_clean, media_urls)

    # ============================================================
    #  MONDAY FAILSAFE (KEY FIX)
    #  If the model didn't emit JSON, we still send lead_info when
    #  we already have the required fields in context.
    # ============================================================
    if lead_info is None:
        candidate = {
            "nombre": saved_name,
            "interes": last_interest,
            "cita": last_appointment,
            "pago": last_payment or "Por definir",
        }

        if _lead_is_valid(candidate):
            lead_info = candidate
        else:
            # If user sends a short confirmation after appointment negotiation,
            # retry with stored appointment info.
            if _message_confirms_appointment(user_message) and saved_name and last_interest and last_appointment:
                if _lead_is_valid(candidate):
                    lead_info = candidate

    # Persist history and context
    new_history = (history + f"\nC: {user_message}\nA: {reply_clean}").strip()
    new_context = {
        "history": new_history[-4000:],
        "user_name": saved_name,
        "last_interest": last_interest,
        "last_appointment": last_appointment,
        "last_payment": last_payment,
    }

    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": new_context,
        "media_urls": media_urls,
        "lead_info": lead_info,
    }


