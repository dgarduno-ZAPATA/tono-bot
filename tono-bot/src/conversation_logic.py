import os
import re
import logging
from typing import Dict, Any, List
from openai import OpenAI

logger = logging.getLogger(__name__)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# === PERSONALIDAD: ADRIAN (ASESOR EXPERTO) ===
SYSTEM_PROMPT = """
Eres "Adrian", Asesor Comercial de 'Tractos y Max'.

OBJETIVO:
1. Resolver dudas sobre el inventario.
2. PERFILAR al cliente (Uso y Forma de Pago).
3. CERRAR LA CITA (Fecha y Hora).

TU BIBLIA (REGLAS INQUEBRANTABLES):
1. **TIENES TODO EL INVENTARIO:** Si el cliente pide un modelo y está en la lista que te paso, ¡VÉNDELO! No digas que no lo tienes.
2. **CERO SALUDOS REPETITIVOS:** Si el historial muestra que ya saludaste, NO vuelvas a decir "Hola". Ve directo a la respuesta.
3. **DIRECCIÓN:** Av. de los Camioneros 123. (DILA SOLO SI TE LA PREGUNTAN o al confirmar cita).
4. **NO INVENTES:** Si te piden algo que DE VERDAD no está en la lista completa, ofrece una alternativa similar.

ESTILO:
- Profesional, amable y conciso.
- Máximo 3 oraciones por respuesta.
"""

def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def _build_inventory_text(inventory_service) -> str:
    """
    SIN FILTROS: Pasa el inventario COMPLETO a la IA.
    Son pocas filas (28), así que la IA puede leerlo todo sin problemas.
    """
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return "No hay inventario disponible."

    lines = []
    # Recorremos TODO el inventario (sin [:15])
    for item in items: 
        marca = _safe_get(item, ["Marca", "marca"])
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"])
        anio = _safe_get(item, ["Anio", "Año", "anio"])
        precio = _safe_get(item, ["Precio", "precio"])
        status = _safe_get(item, ["status", "disponible"], default="Disponible")
        desc = _safe_get(item, ["descripcion_corta", "segmento"], default="")

        label = f"{marca} {modelo} {anio}".strip()
        info = f"- {label}: ${precio} ({status}) [{desc}]"
        
        lines.append(info)

    return "\n".join(lines)

def _extract_photos_from_item(item: dict) -> List[str]:
    raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes", "fotos"])
    if not raw: return []
    return [u.strip() for u in raw.split("|") if u.strip().startswith("http")]

def _pick_media_urls(user_message: str, reply: str, inventory_service) -> List[str]:
    """Busca fotos coincidiendo palabras clave del modelo."""
    items = getattr(inventory_service, "items", None) or []
    if not items: return []

    msg = user_message.lower()
    rep = reply.lower()
    
    # Palabras clave forzadas (para arreglar typos del usuario)
    # Si el usuario escribe "miller", entendemos "miler"
    msg = msg.replace("miller", "miler").replace("vanesa", "van")

    for item in items:
        urls = _extract_photos_from_item(item)
        if not urls: continue

        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).lower()
        
        # Tokenizamos el modelo (ej: "tunland g9" -> "tunland", "g9")
        parts = modelo.split()
        
        # Si alguna parte CLAVE del modelo está en el mensaje, mandamos foto
        for part in parts:
            if len(part) < 2 or part in ["foton", "camion"]: continue
            
            if part in msg:
                return urls # Búsqueda directa en lo que dijo el cliente
            
            if part in rep:
                return urls # Búsqueda en lo que respondió la IA

    return []

def handle_message(user_message, inventory_service, state, context):
    # 1. Pasamos TODO el inventario
    inventory_text = _build_inventory_text(inventory_service)
    history = (context.get("history") or "").strip()

    context_block = f"""
LISTA COMPLETA DE INVENTARIO:
{inventory_text}

HISTORIAL DE CHAT:
{history[-2000:] if history else "Inicio."}
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context_block}, 
        {"role": "user", "content": user_message}
    ]

    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo", 
            messages=messages,
            temperature=0.4, # Precisión alta
            max_tokens=250,
        )
        reply = resp.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        reply = "Dame un momento... (Error técnico)"

    # Limpieza
    reply_clean = re.sub(r"^(Adrian|Asesor|Bot):", "", reply.strip(), flags=re.IGNORECASE).strip()
    
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)

    new_history = history + f"\nCliente: {user_message}\nAdrian: {reply_clean}"
    
    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": {"history": new_history[-3000:]},
        "media_urls": media_urls
    }
