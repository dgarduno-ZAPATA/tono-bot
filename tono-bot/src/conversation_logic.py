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

# Modelo
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

# === PERSONALIDAD: ADRIAN (CON RELOJ) ===
SYSTEM_PROMPT = """
Eres "Adrian", Asesor Comercial de 'Tractos y Max'.

OBJETIVO: Vender camiones y agendar visitas.

DATOS CLAVE:
- Ubicación: Av. de los Camioneros 123 (Portón Azul).
- Horario: Lunes a Viernes 9:00 AM a 6:00 PM. Sábados 9:00 AM a 2:00 PM.
- MOMENTO ACTUAL: {current_time_str}

REGLAS OBLIGATORIAS:
1. REVISA EL RELOJ:
   - Antes de responder, mira el "MOMENTO ACTUAL".
   - Si es FUERA de horario, responde amablemente que la oficina está cerrada, pero ofrece tomar sus datos o agendar para mañana a primera hora.

2. MODO SILENCIO: Si el usuario escribe "/silencio", confirma brevemente y deja de responder.

3. DETECTAR LEAD (CRÍTICO): Si logras concertar una cita (tienes NOMBRE + DÍA/HORA),
   debes incluir al final de tu respuesta un JSON oculto en este formato exacto:

   ```json
   {{
       "lead_event": {{
           "nombre": "Juan Perez",
           "interes": "Foton G9",
           "cita": "Viernes 10am",
           "pago": "Contado"
       }}
   }}

```

4. NO REPETIR: No repitas saludos ("Hola") ni direcciones si ya las diste hace poco.
5. INVENTARIO: Vende solo lo que ves en la lista. Si no está, ofrece alternativas similares.
6. MODO GPS: Si te piden ubicación, dales la dirección exacta y una referencia visual, no mandes fotos del inventario.

ESTILO: Amable, directo y profesional. Máximo 3 oraciones.
"""

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

```
lines: List[str] = []
for item in items:
    marca = _safe_get(item, ["Marca", "marca"], default="(sin marca)")
    modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"], default="(sin modelo)")
    anio = _safe_get(item, ["Anio", "Año", "anio"], default="")
    precio = _safe_get(item, ["Precio", "precio"], default="N/D")
    status = _safe_get(item, ["status", "disponible"], default="Disponible")
    desc = _safe_get(item, ["descripcion_corta", "segmento"], default="")

    info = f"- {marca} {modelo} {anio}: ${precio} ({status})"
    if desc: 
        info += f" [{desc}]"
    lines.append(info)

return "\n".join(lines)

```

def _extract_photos_from_item(item: Dict[str, Any]) -> List[str]:
raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes", "fotos"])
if not raw:
return []
return [u.strip() for u in raw.split("|") if u.strip().startswith("http")]

# === LÓGICA DE FOTOS BLINDADA (VERSIÓN CORRECTA) ===

def _pick_media_urls(user_message: str, reply: str, inventory_service) -> List[str]:
msg = (user_message or "").lower()

```
# 1) FILTRO GPS
gps_keywords = [
    "ubicacion", "ubicación", "donde estan", "dónde están",
    "direccion", "dirección", "mapa", "donde se ubican"
]
if any(k in msg for k in gps_keywords):
    return []

items = getattr(inventory_service, "items", None) or []
if not items:
    return []

# 2) NORMALIZACIÓN
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

# 3) REGLA DE ORO (El Gatekeeper):
photo_keywords = [
    "foto", "fotos", "imagen", "imagenes", "imágenes",
    "ver fotos", "ver imágenes",
    "enseñame", "enséñame", "muestrame", "muéstrame",
    "mandame", "mándame", "quiero ver", 
    "verla", "verlo", "ver el", "ver la",
    "conocerla", "conocerlo"
]

# 🔥 BLOQUEO TOTAL: Si no hay intención explícita de ver, cortamos aquí.
if not any(k in msg_norm for k in photo_keywords):
    return []  

# 4) Si pidió fotos, buscamos a qué unidad se refiere
for item in items:
    urls = _extract_photos_from_item(item)
    if not urls:
        continue

    modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).lower().strip()
    if not modelo:
        continue

    parts = modelo.split()

    # Match por el mensaje del usuario (Prioridad 1)
    match_user = any(
        part in msg_norm
        for part in parts
        if len(part) >= 3 and part not in ["foton", "camion", "camión"]
    )

    # Match por contexto del bot (Prioridad 2 - Contexto)
    match_bot = any(
        part in rep_norm
        for part in parts
        if len(part) >= 3 and part not in ["foton", "camion", "camión"]
    )

    # Si pidió fotos + mencionó modelo -> MANDA
    if match_user:
        return urls

    # Si pidió fotos pero no dijo cual, usamos lo que el bot estaba ofreciendo -> MANDA
    if match_bot:
        return urls

return []

```

def handle_message(user_message: str, inventory_service, state: str, context: Dict[str, Any]) -> Dict[str, Any]:
user_message = user_message or ""
context = context or {}
history = (context.get("history") or "").strip()

```
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

# === HORA REAL ===
_, current_time_str = get_mexico_time()
formatted_system_prompt = SYSTEM_PROMPT.format(current_time_str=current_time_str)

inventory_text = _build_inventory_text(inventory_service)

context_block = (
    f"MOMENTO ACTUAL: {current_time_str}\n"
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

    # === EXTRACCIÓN DE LEAD (MONDAY) ===
    json_match = re.search(r"```json\s*({.*?})\s*```", raw_reply, re.DOTALL)
    if json_match:
        try:
            lead_data = json.loads(json_match.group(1))
            if isinstance(lead_data, dict) and "lead_event" in lead_data:
                lead_info = lead_data["lead_event"]
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()
        except Exception:
            pass

except Exception as e:
    logger.error(f"Error OpenAI: {e}")
    reply_clean = "Dame un momento, estoy consultando sistema..."

reply_clean = re.sub(r"^(Adrian|Asesor|Bot)\s*:\s*", "", reply_clean.strip(), flags=re.IGNORECASE).strip()

media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)
new_history = (history + f"\nC: {user_message}\nA: {reply_clean}").strip()

return {
    "reply": reply_clean,
    "new_state": "chatting",
    "context": {"history": new_history[-4000:]},
    "media_urls": media_urls,
    "lead_info": lead_info,
}
