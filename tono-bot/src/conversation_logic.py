import os
import re
import json
import logging
from typing import Dict, Any, List, Optional
from openai import OpenAI

logger = logging.getLogger(__name__)

# === CONFIGURACIÓN DE IA ===
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Usamos el modelo optimizado (Más rápido y barato)
MODEL_NAME = "gpt-4o-mini"

# === PERSONALIDAD: ADRIAN (ASESOR EXPERTO) ===
SYSTEM_PROMPT = """
Eres "Adrian", Asesor Comercial de 'Tractos y Max'.

OBJETIVO: Vender camiones y agendar visitas.

DATOS CLAVE:
- Ubicación: Av. de los Camioneros 123 (Portón Azul).
- Horario: Lunes a Viernes 9am a 6pm.

REGLAS OBLIGATORIAS:
1. MODO SILENCIO: Si el usuario escribe "/silencio", confirma brevemente y deja de responder.
2. DETECTAR LEAD (CRÍTICO): Si logras concertar una cita (tienes NOMBRE + DÍA/HORA),
   debes incluir al final de tu respuesta un JSON oculto en este formato exacto,
   dentro de un bloque ```json ... ```:

   {"lead_event": {"nombre": "Juan Perez", "interes": "Foton G9", "cita": "Viernes 10am", "pago": "Contado"}}

   (El usuario no verá esto, pero el sistema sí lo leerá para guardarlo en el CRM).

3. NO REPETIR: No repitas saludos ("Hola") ni direcciones si ya las diste hace poco.
4. INVENTARIO: Vende solo lo que ves en la lista. Si no está, ofrece alternativas similares.
5. MODO GPS: Si te piden ubicación, dales la dirección exacta y una referencia visual,
   no mandes fotos del inventario.

ESTILO: Amable, directo y profesional. Máximo 3 oraciones.
"""


def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
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

    # Leemos TODO el inventario (sin límite de filas)
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


def _extract_photos_from_item(item: Dict[str, Any]) -> List[str]:
    raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes", "fotos"])
    if not raw:
        return []
    return [u.strip() for u in raw.split("|") if u.strip().startswith("http")]


def _pick_media_urls(user_message: str, reply: str, inventory_service) -> List[str]:
    msg = (user_message or "").lower()

    # REGLA DE ORO: Si piden ubicación, PROHIBIDO mandar fotos de camiones
    if any(x in msg for x in ["ubicacion", "ubicación", "donde estan", "dónde están", "direccion", "dirección", "mapa", "donde se ubican"]):
        return []

    items = getattr(inventory_service, "items", None) or []
    if not items:
        return []

    rep = (reply or "").lower()

    # Corrección de typos comunes
    msg = (
        msg.replace("miller", "miler")
           .replace("vanesa", "toano")
           .replace("la e5", "tunland e5")
    )

    for item in items:
        urls = _extract_photos_from_item(item)
        if not urls:
            continue

        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).lower()
        parts = modelo.split()

        for part in parts:
            if len(part) < 3 or part in ["foton", "camion", "camión"]:
                continue

            # Si el modelo está en el mensaje del usuario O en la respuesta del bot
            if part in msg or part in rep:
                return urls

    return []


def handle_message(user_message: str, inventory_service, state: str, context: Dict[str, Any]) -> Dict[str, Any]:
    user_message = user_message or ""
    context = context or {}

    # === MODO SILENCIO (hard rule desde backend) ===
    if user_message.strip().lower() == "/silencio":
        new_history = (context.get("history") or "") + f"\nC: {user_message}\nA: Perfecto. Modo silencio activado."
        return {
            "reply": "Perfecto. Modo silencio activado.",
            "new_state": "silent",
            "context": {"history": new_history[-4000:]},
            "media_urls": [],
            "lead_info": None,
        }

    # Si ya está en modo silencio, no responde nada
    if state == "silent":
        return {
            "reply": "",
            "new_state": "silent",
            "context": context,
            "media_urls": [],
            "lead_info": None,
        }

    inventory_text = _build_inventory_text(inventory_service)
    history = (context.get("history") or "").strip()

    # Preparamos el mensaje para la IA
    context_block = (
        f"INVENTARIO DISPONIBLE:\n{inventory_text}\n\n"
        f"HISTORIAL DE CHAT:\n{history[-3000:]}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context_block},
        {"role": "user", "content": user_message},
    ]

    lead_info: Optional[Dict[str, Any]] = None
    reply_clean = "Hubo un error técnico."

    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.3,  # Baja temperatura = más obediente
            max_tokens=350,
        )

        raw_reply = resp.choices[0].message.content or ""

        # === EXTRACCIÓN DE LEAD (MONDAY CRM) ===
        json_match = re.search(r"```json\s*({.*?})\s*```", raw_reply, re.DOTALL)
        reply_clean = raw_reply

        if json_match:
            try:
                lead_data = json.loads(json_match.group(1))
                if isinstance(lead_data, dict) and "lead_event" in lead_data:
                    lead_info = lead_data["lead_event"]
                    reply_clean = raw_reply.replace(json_match.group(0), "").strip()
            except Exception:
                # Si falla el JSON, seguimos normal sin romper el flujo
                pass

    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        reply_clean = "Dame un momento, estoy consultando sistema..."

    # Limpieza final de texto (Quitar "Adrian:" si la IA lo puso)
    reply_clean = re.sub(r"^(Adrian|Asesor|Bot)\s*:\s*", "", reply_clean.strip(), flags=re.IGNORECASE).strip()

    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)

    new_history = (history + f"\nC: {user_message}\nA: {reply_clean}").strip()

    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": {"history": new_history[-4000:]},
        "media_urls": media_urls,
        "lead_info": lead_info,  # <--- Este dato viaja a main.py para irse a Monday
    }
