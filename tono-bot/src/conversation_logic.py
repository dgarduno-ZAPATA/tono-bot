import os
import re
import logging
from typing import Dict, Any, List
from openai import OpenAI

logger = logging.getLogger(__name__)

# === CLIENTE OPENAI (SDK NUEVO) ===
# Asegúrate de tener OPENAI_API_KEY en tus variables de entorno
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# === PERSONALIDAD "TOÑO" ===
SYSTEM_PROMPT = """
Eres "Toño", el vendedor estrella de 'Tractos y Max'.
Tu objetivo es VENDER camiones.

Personalidad:
- Camionero experto: "Puro fierro", "Listo para la chamba", "Jala durísimo", "Unidad al 100".
- Agresivo pero amable: "¿Te lo aparto?", "¿Cuándo vienes?".
- Visual: usa emojis 🚛🔥🛠️💰.

Reglas:
1. Si preguntan precio: dalo y pregunta si hacen trato.
2. Si preguntan "¿qué tienes?": ofrece 2-3 opciones y pregunta cuál le late.
3. Si no hay lo que piden: "Se me acaba de ir, pero tengo estos otros fierros..."
4. Respuestas MÁXIMO 3 oraciones. Corto y directo.
"""

def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    """Busca valor en varias llaves posibles para evitar errores."""
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def _build_inventory_text(inventory_service) -> str:
    """Convierte el inventario en texto para que la IA lo lea."""
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return "No hay inventario disponible por el momento."

    lines = []
    # Limitamos a 15 unidades para no gastar demasiados tokens de IA
    for item in items[:15]: 
        marca = _safe_get(item, ["Marca", "marca", "BRAND"])
        modelo = _safe_get(item, ["Modelo", "modelo", "MODEL"])
        anio = _safe_get(item, ["Anio", "Año", "anio", "year"])
        precio = _safe_get(item, ["Precio", "precio", "price"])
        status = _safe_get(item, ["status", "Estado", "disponible"], default="Disponible")

        label = f"{marca} {modelo} {anio}".strip() or "Unidad"
        
        if precio:
            lines.append(f"- {label}: ${precio} ({status})")
        else:
            lines.append(f"- {label} ({status})")

    return "\n".join(lines)

def _trim_to_3_sentences(text: str) -> str:
    """Recorta la respuesta para que no sea una biblia de texto."""
    text = (text or "").strip()
    if not text: return ""
    
    # Divide por puntos o signos de cierre
    parts = re.split(r'(?<=[.!?])\s+', text)
    trimmed = " ".join(parts[:3]).strip()
    
    # Corte de seguridad
    if len(trimmed) > 400:
        trimmed = trimmed[:400].rstrip() + "..."
    return trimmed

def _extract_photos_from_item(item: dict) -> List[str]:
    """Extrae fotos soportando múltiples links separados por '|'."""
    raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes"])
    if not raw:
        return []
    
    # Aquí está la magia que recuperamos de tu código anterior:
    # Separa por '|', limpia espacios y filtra solo lo que parezca link.
    urls = [u.strip() for u in raw.split("|") if u.strip().startswith("http")]
    return urls

def _pick_media_urls(user_message: str, reply: str, inventory_service) -> List[str]:
    """
    Busca fotos inteligentes. 
    Si el usuario o el bot mencionan un modelo, devolvemos SUS fotos.
    """
    items = getattr(inventory_service, "items", None) or []
    if not items: return []

    msg = user_message.lower()
    rep = reply.lower()
    
    for item in items:
        # Extraemos las URLs de este camión
        urls = _extract_photos_from_item(item)
        if not urls: continue

        modelo = _safe_get(item, ["Modelo", "modelo"]).lower()
        marca = _safe_get(item, ["Marca", "marca"]).lower()

        # LOGICA DE COINCIDENCIA:
        # Si el modelo (ej: "t680", "cascadia") aparece en lo que escribió el cliente
        # O en lo que contestó el bot, asumimos que estamos hablando de ese camión.
        if modelo and len(modelo) > 2:
            if modelo in msg or modelo in rep:
                return urls # Devolvemos TODAS las fotos de ese camión (lista)

    return []

def handle_message(user_message, inventory_service, state, context):
    # 1. Preparamos el contexto
    inventory_text = _build_inventory_text(inventory_service)
    history = (context.get("history") or "").strip()

    # Contexto para la IA (separado de las instrucciones para evitar hackeos)
    context_block = f"""
INVENTARIO ACTUAL:
{inventory_text}

HISTORIAL DE CHARLA:
{history[-1000:] if history else "Inicio."}
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context_block}, 
        {"role": "user", "content": user_message}
    ]

    # 2. Llamada a OpenAI
    try:
        # Usamos gpt-3.5-turbo (o gpt-4o-mini si prefieres)
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo", 
            messages=messages,
            temperature=0.7,
            max_tokens=250,
        )
        reply = resp.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        reply = "Dame un segundo, se me cayó la señal... 📶 (Error técnico)"

    # 3. Post-procesamiento
    reply_clean = _trim_to_3_sentences(reply)
    
    # Aquí buscamos las fotos (soporta múltiples)
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)

    # 4. Actualizar historial
    new_history = history + f"\nC: {user_message}\nT: {reply_clean}"
    
    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": {"history": new_history[-2000:]}, 
        "media_urls": media_urls
    }
