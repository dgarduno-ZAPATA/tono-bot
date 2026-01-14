import re
from src.ai_reply import generate_reply

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _extract_choice(text: str) -> int | None:
    t = _norm(text)
    m = re.search(r"\b(1|2)\b", t)
    if not m:
        return None
    return int(m.group(1)) - 1

def _get_photo_urls(unit: dict) -> list[str]:
    raw = (unit.get("photos") or "").strip()
    if not raw:
        return []
    urls = [u.strip() for u in raw.split("|") if u.strip()]
    return urls[:3]

def handle_message(message, inventory_service, state, context):
    inv = inventory_service.items if hasattr(inventory_service, "items") else []
    context = context or {}
    state = state or "start"

    user_text = (message or "").strip()
    t = _norm(user_text)

    # Si eligió opción 1/2, enfoca esa opción
    choice = _extract_choice(user_text)
    if choice is not None and isinstance(context.get("last_options"), list):
        last_options = context["last_options"]
        if 0 <= choice < len(last_options):
            context["focused_model"] = last_options[choice]
            state = "detail"

    focused = context.get("focused_model")

    # Fotos
    wants_photos = any(w in t for w in ["foto", "fotos", "imagen", "imagenes", "imágenes"])
    if wants_photos:
        if focused:
            urls = _get_photo_urls(focused)
            if urls:
                return {
                    "reply": "Claro ✅ Te comparto fotos. ¿Quieres verla hoy o mañana?",
                    "new_state": "photos",
                    "context": context,
                    "media_urls": urls
                }
            return {
                "reply": "Aún no tengo fotos cargadas de esa unidad 🙏 ¿Quieres que te comparta otras opciones?",
                "new_state": "no_photos",
                "context": context
            }
        return {
            "reply": "Claro ✅ ¿De cuál opción quieres fotos? (responde 1 o 2)",
            "new_state": "need_photo_choice",
            "context": context
        }

    # Saludo corto
    if any(w in t for w in ["hola", "buenas", "buen día", "buen dia", "buenas tardes", "buenas noches", "que tal", "qué tal"]):
        return {"reply": "¡Hola! ¿Qué modelo te interesa o qué buscas?", "new_state": "greeting", "context": context}

    # IA
    result = generate_reply(user_text, inv, {"state": state, **context})
    if not isinstance(result, dict):
        return {"reply": "¿Qué modelo te interesa o qué buscas?", "new_state": "no_match", "context": context}

    reply = (result.get("reply") or "").strip()
    idxs = result.get("selected_indexes") or []
    new_state = result.get("new_state") or state

    # Construir opciones reales
    options = []
    for i in idxs[:2]:
        if isinstance(i, int) and 0 <= i < len(inv):
            options.append(inv[i])

    if options:
        context["last_options"] = options
        context["focused_model"] = options[0]

    if not reply:
        reply = "¿Qué modelo te interesa o qué buscas?"
        new_state = "no_match"

    return {"reply": reply, "new_state": str(new_state), "context": context}
