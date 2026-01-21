import os
import re
import logging
from typing import Dict, Any, List
from openai import OpenAI

logger = logging.getLogger(__name__)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# === PERSONALIDAD: ADRIAN (ASESOR INTELIGENTE) ===
SYSTEM_PROMPT = """
Eres "Adrian", Asesor Comercial de 'Tractos y Max'.
Tu meta es PERFILAR y CERRAR CITA.

UBICACIÓN: Av. de los Camioneros 123.

REGLAS DE ORO (COMPORTAMIENTO):
1. **MEMORIA:** - Si el cliente ya dijo para qué la quiere, NO preguntes de nuevo.
   - Si el cliente ya dijo su nombre, úsalo pero NO vuelvas a saludar ("Hola...").
   - Si ya acordaron una hora, solo confirma, no vuelvas a preguntar "¿cuándo?".

2. **CERO SALUDOS REPETITIVOS:**
   - Si ya hay conversación, ve directo al punto.
   - MAL: "Hola Jonas, claro que sí..."
   - BIEN: "Claro Jonas, para ese trabajo te recomiendo..."

3. **BUSCADOR DE OPORTUNIDADES:**
   - Si preguntan por un modelo específico (ej. "G9") y está en tu lista, OFRÉCELO con precio y un beneficio.
   - Si NO está, ofrece la alternativa más cercana.

4. **CIERRE DE CITA:**
   - Objetivo: Fecha y Hora concreta.
   - Si preguntan ubicación, dales la dirección.

FORMATO: Profesional, amable, corto (máx 3 oraciones).
"""

def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def _filter_inventory(inventory_service, user_query: str) -> List[dict]:
    """
    FILTRO INTELIGENTE:
    Si el cliente pide "G9", mueve las G9 al principio de la lista
    para que Adrian las vea de inmediato.
    """
    items = getattr(inventory_service, "items", None) or []
    if not items: return []
    
    query = user_query.lower()
    prioritized = []
    others = []

    for item in items:
        # Buscamos en todo el contenido del camión
        full_text = " ".join(str(v).lower() for v in item.values())
        
        # Si encuentra palabras clave (ej: "g9", "pickup", "toano")
        is_relevant = False
        words = query.split()
        for w in words:
            if len(w) > 1 and w in full_text:
                is_relevant = True
                break
        
        if is_relevant:
            prioritized.append(item)
        else:
            others.append(item)

    return prioritized + others

def _build_inventory_text(inventory_service, user_query: str) -> str:
    """Construye el inventario priorizando lo que el cliente quiere."""
    # 1. Obtenemos la lista ordenada (lo relevante primero)
    sorted_items = _filter_inventory(inventory_service, user_query)
    
    if not sorted_items:
        return "No hay inventario disponible."

    lines = []
    # 2. Le pasamos los primeros 15 a la IA (ahora sí incluirá la G9 si la pidieron)
    for item in sorted_items[:15]: 
        marca = _safe_get(item, ["Marca", "marca"])
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"])
        anio = _safe_get(item, ["Anio", "Año", "anio"])
        precio = _safe_get(item, ["Precio", "precio"])
        status = _safe_get(item, ["status", "disponible"], default="Disponible")
        desc = _safe_get(item, ["descripcion_corta", "segmento"], default="")

        label = f"{marca} {modelo} {anio}".strip()
        info = f"- {label}: ${precio} ({status})"
        if desc: info += f" [{desc}]"
        
        lines.append(info)

    return "\n".join(lines)

def _extract_photos_from_item(item: dict) -> List[str]:
    raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes", "fotos"])
    if not raw: return []
    return [u.strip() for u in raw.split("|") if u.strip().startswith("http")]

def _pick_media_urls(user_message: str, reply: str, inventory_service) -> List[str]:
    items = getattr(inventory_service, "items", None) or []
    if not items: return []

    msg = user_message.lower()
    rep = reply.lower()
    
    # Palabras clave fuertes para forzar búsqueda de fotos
    target_keywords = []
    if "g9" in msg: target_keywords.append("g9")
    if "g7" in msg: target_keywords.append("g7")
    if "toano" in msg: target_keywords.append("toano")
    if "e5" in msg: target_keywords.append("e5")
    if "miler" in msg: target_keywords.append("miler")

    for item in items:
        urls = _extract_photos_from_item(item)
        if not urls: continue

        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).lower()
        
        # 1. Coincidencia directa fuerte
        for kw in target_keywords:
            if kw in modelo:
                return urls

        # 2. Coincidencia general
        if modelo and len(modelo) > 3 and (modelo in msg or modelo in rep):
            return urls

    return []

def handle_message(user_message, inventory_service, state, context):
    # Inventario filtrado por lo que pide el cliente
    inventory_text = _build_inventory_text(inventory_service, user_message)
    history = (context.get("history") or "").strip()

    context_block = f"""
INVENTARIO RELEVANTE:
{inventory_text}

HISTORIAL:
{history[-1500:] if history else "Inicio."}
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
            temperature=0.5, 
            max_tokens=200,
        )
        reply = resp.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        reply = "Dame un momento, consulto sistema... (Error técnico)"

    # Limpiamos si la IA alucina el nombre al principio
    reply_clean = re.sub(r"^(Adrian|Toño|Asesor|Bot):", "", reply.strip(), flags=re.IGNORECASE).strip()
    
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)

    new_history = history + f"\nCliente: {user_message}\nAdrian: {reply_clean}"
    
    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": {"history": new_history[-2500:]},
        "media_urls": media_urls
    }
