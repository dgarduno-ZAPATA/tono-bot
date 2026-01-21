import os
import re
import logging
from typing import Dict, Any, List
from openai import OpenAI

logger = logging.getLogger(__name__)

# Cliente OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# === EL NUEVO CEREBRO: ASESOR CONSULTIVO (ADRIAN 3.0) ===
SYSTEM_PROMPT = """
Eres "Adrian", un Asesor Comercial Profesional de 'Tractos y Max'.
Tu objetivo NO es solo dar precios, sino **PERFILAR** al cliente y **AGENDAR UNA CITA REAL** en la sucursal.

TU PERSONALIDAD:
- Profesional y confiable (NO uses frases de marketing barato como "vendedor estrella").
- Directo y servicial.
- Usas emojis con moderación (MÁXIMO 1 por mensaje).

TU PROCESO DE VENTA (Sigue este orden lógico):

1. **DIAGNÓSTICO (Vital):** Antes de soltar precios a lo loco o pedir el cierre, intenta saber:
   - ¿Para qué trabajo la necesitan? (Reparto, carga pesada, personal).
   - ¿Buscan contado o financiamiento?

2. **PROPUESTA DE VALOR:**
   - Cuando des un precio, menciona UN beneficio clave basado en su uso.
   - Ejemplo: "La Toano Panel vale $720k. Por su espacio es ideal para paquetería urbana."

3. **CIERRE DE CITA (Protocolo Estricto):**
   - Si el cliente muestra interés o dice "voy a ir", NO digas solo "te espero".
   - **Debes concretar:** "¿Te acomoda mejor por la mañana o por la tarde?"
   - **Debes pedir datos:** "¿A nombre de quién registro la visita?"
   - Ubicación: "Estamos en Av. de los Camioneros 123".

REGLAS DE ORO (Constraints):
- 🚫 PROHIBIDO decir "¿Hacemos trato?" en el primer o segundo mensaje. Eso espanta al cliente.
- Si preguntan "Precio", dalo, pero termina con una pregunta de perfilado: "¿Este modelo es para uso personal o negocio?".
- Si el inventario está vacío, ofrece ayuda para buscar la unidad.
- Respuestas cortas y humanas (máximo 3 oraciones).
"""

def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    """Busca valor en varias llaves posibles para evitar errores."""
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def _build_inventory_text(inventory_service) -> str:
    """Crea un resumen del inventario con datos útiles para el perfilado."""
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return "No hay inventario disponible por el momento."

    lines = []
    # Limitamos a 15 para no saturar, pero incluimos Segmento/Descripción si existen
    for item in items[:15]: 
        marca = _safe_get(item, ["Marca", "marca", "BRAND"])
        modelo = _safe_get(item, ["Modelo", "modelo", "MODEL"])
        anio = _safe_get(item, ["Anio", "Año", "anio", "year"])
        precio = _safe_get(item, ["Precio", "precio", "price"])
        status = _safe_get(item, ["status", "Estado", "disponible"], default="Disponible")
        
        # Intentamos buscar info extra para que el bot tenga "micro-valor"
        desc = _safe_get(item, ["descripcion_corta", "segmento", "Descripcion"], default="")

        label = f"{marca} {modelo} {anio}".strip() or "Unidad"
        
        info_line = f"- {label}: ${precio} ({status})"
        if desc:
            info_line += f" [Ideal para: {desc}]"
            
        lines.append(info_line)

    return "\n".join(lines)

def _trim_response(text: str) -> str:
    """Limpieza de respuesta para WhatsApp."""
    text = (text or "").strip()
    if not text: return ""
    
    # Si la IA genera bloques de "T:", "Toño:", los quitamos
    text = re.sub(r"^(Toño|T|Bot):", "", text, flags=re.IGNORECASE).strip()

    # Divide por puntos para no mandar biblias
    parts = re.split(r'(?<=[.!?])\s+', text)
    if len(parts) > 3:
        trimmed = " ".join(parts[:3]).strip()
    else:
        trimmed = text
        
    return trimmed

def _extract_photos_from_item(item: dict) -> List[str]:
    """Extrae fotos soportando múltiples links separados por '|'."""
    raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes"])
    if not raw:
        return []
    urls = [u.strip() for u in raw.split("|") if u.strip().startswith("http")]
    return urls

def _pick_media_urls(user_message: str, reply: str, inventory_service) -> List[str]:
    """Busca fotos si el contexto lo amerita."""
    items = getattr(inventory_service, "items", None) or []
    if not items: return []

    msg = user_message.lower()
    rep = reply.lower()
    
    for item in items:
        urls = _extract_photos_from_item(item)
        if not urls: continue

        modelo = _safe_get(item, ["Modelo", "modelo"]).lower()
        
        # Si el modelo está en la charla, mandamos foto
        if modelo and len(modelo) > 2:
            if modelo in msg or modelo in rep:
                return urls 

    return []

def handle_message(user_message, inventory_service, state, context):
    # 1. Preparamos el contexto
    inventory_text = _build_inventory_text(inventory_service)
    history = (context.get("history") or "").strip()

    # Contexto separado para evitar inyección de prompt
    context_block = f"""
INVENTARIO DISPONIBLE (Úsalo para recomendar):
{inventory_text}

HISTORIAL RECIENTE:
{history[-1000:] if history else "Inicio de conversación."}
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": context_block}, 
        {"role": "user", "content": user_message}
    ]

    # 2. Llamada a OpenAI
    try:
        # Recomendación: Usar gpt-4o-mini si es posible, sigue mejor las instrucciones de perfilado
        # Si no, gpt-3.5-turbo está bien.
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo", 
            messages=messages,
            temperature=0.6, # Bajamos temperatura para que sea más serio/obediente
            max_tokens=250,
        )
        reply = resp.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        reply = "Dame un momento, estoy verificando la información... (Error de sistema)"

    # 3. Post-procesamiento
    reply_clean = _trim_response(reply)
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)

    # 4. Actualizar historial
    new_history = history + f"\nCliente: {user_message}\nToño: {reply_clean}"
    
    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": {"history": new_history[-2000:]}, 
        "media_urls": media_urls
    }

