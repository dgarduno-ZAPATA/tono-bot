import os
import re
import logging
from typing import Dict, Any, List
from openai import OpenAI

logger = logging.getLogger(__name__)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# === PERSONALIDAD: ADRIAN (ASESOR HUMANO V5) ===
SYSTEM_PROMPT = """
Eres "Adrian", Asesor Comercial de 'Tractos y Max'.
Tu trabajo es conversar naturalmente, resolver dudas y concretar visitas.

DATOS OPERATIVOS (Apréndetelos):
- **Ubicación:** Av. de los Camioneros 123.
- **Horario:** Lunes a Viernes de 9:00 AM a 6:00 PM. (Sábados hasta las 2pm).
- **Si preguntan "¿Por quién pregunto?":** "Pregunta por mí, Adrian".
- **Si preguntan Fotos:** Di "Claro, aquí tienes la foto" (El sistema la enviará por ti).

REGLAS DE ORO (ANTI-ROBOT):
1. **PROHIBIDO REPETIR:** Si ya dijiste "Foton Tunland E5 2024 a $300k", en los siguientes mensajes solo di "la camioneta", "la unidad" o "la E5". ¡No repitas el nombre completo y precio en cada respuesta! Cansa al cliente.
2. **ESCUCHA PRIMERO:**
   - Si preguntan "¿A qué hora cierran?", responde el HORARIO de cierre (6 PM), no preguntes por la cita.
   - Si preguntan "¿Con qué banco?", busca en el inventario la columna 'Banco' (ej. Banorte) y díselo.
   - Si dicen "No tengo banco", ofrece el "Crédito Directo" si el inventario lo menciona.
3. **NATURALIDAD:** Si el cliente te insulta o se desespera, ofrece disculpas cortas y ve al grano.
4. **NO SALUDES SIEMPRE:** Si ya están hablando, no digas "Hola" ni "Perfecto" en cada mensaje.

FORMATO: Respuesta corta y directa.
"""

def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def _build_inventory_text(inventory_service) -> str:
    """
    Pasa TODO el inventario, incluyendo datos de Financiamiento y Bancos.
    """
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return "No hay inventario disponible."

    lines = []
    for item in items: 
        marca = _safe_get(item, ["Marca", "marca"])
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"])
        anio = _safe_get(item, ["Anio", "Año", "anio"])
        precio = _safe_get(item, ["Precio", "precio"])
        status = _safe_get(item, ["status", "disponible"], default="Disponible")
        
        # AGREGAMOS DATOS FINANCIEROS CLAVE
        banco = _safe_get(item, ["Banco", "banco", "Financiera"], default="Varios bancos")
        tipo_fin = _safe_get(item, ["Tipo de financiamiento", "Financiamiento"], default="Crédito disponible")
        
        label = f"{marca} {modelo} {anio}".strip()
        # Formato compacto para que la IA entienda todo
        info = f"- {label}: ${precio} | Banco sugerido: {banco} | Tipo: {tipo_fin}"
        
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
    
    # Corrección de typos comunes del usuario
    msg = msg.replace("miller", "miler").replace("vanesa", "toano").replace("la e5", "tunland e5")

    for item in items:
        urls = _extract_photos_from_item(item)
        if not urls: continue

        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).lower()
        parts = modelo.split()
        
        for part in parts:
            if len(part) < 2 or part in ["foton", "camion", "de", "el", "la"]: continue
            
            # Si el usuario pide el modelo O si la IA lo menciona en la respuesta
            if part in msg:
                return urls
            if part in rep:
                return urls

    return []

def handle_message(user_message, inventory_service, state, context):
    inventory_text = _build_inventory_text(inventory_service)
    history = (context.get("history") or "").strip()

    context_block = f"""
INVENTARIO COMPLETO (Con Info Bancaria):
{inventory_text}

HISTORIAL:
{history[-2500:] if history else "Inicio."}
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
            temperature=0.3, # Temperatura BAJA para que obedezca las reglas de no repetir
            max_tokens=220,
        )
        reply = resp.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        reply = "Dame un momento... (Error técnico)"

    # Limpieza final
    reply_clean = re.sub(r"^(Adrian|Asesor|Bot):", "", reply.strip(), flags=re.IGNORECASE).strip()
    
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service)

    new_history = history + f"\nCliente: {user_message}\nAdrian: {reply_clean}"
    
    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": {"history": new_history[-3500:]}, # Más memoria
        "media_urls": media_urls
    }
