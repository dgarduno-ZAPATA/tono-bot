import re
from difflib import SequenceMatcher
from src.ai_reply import generate_reply

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()

def pick_options(user_text: str, items: list[dict], limit: int = 2) -> list[dict]:
    """
    Devuelve hasta 2 opciones del inventario, basadas en texto del usuario.
    """
    if not items:
        return []

    t = _norm(user_text)

    # Si el usuario menciona algo parecido a un modelo, lo priorizamos
    scored = []
    for it in items:
        modelo = it.get("Modelo", "") or it.get("modelo", "")
        marca = it.get("Marca", "") or it.get("marca", "")
        key = f"{marca} {modelo}".strip()
        score = _similar(t, key)
        scored.append((score, it))

    scored.sort(key=lambda x: x[0], reverse=True)
    # si el mejor score es muy bajo, consideramos que no entendimos el modelo
    top_score = scored[0][0]
    top_items = [x[1] for x in scored[:limit]] if top_score >= 0.35 else []
    return top_items

def handle_message(message, inventory_service, state, context):
    items = inventory_service.items if hasattr(inventory_service, "items") else []
    text = (message or "").strip()

    # Context defaults
    context = context or {}
    state = state or "start"

    # 1) Si ya hay un "modelo_enfocado" (porque pidió detalle de algo), no cambies de tema
    focused = context.get("focused_model")

    # 2) Si el usuario pide "más detalle" y ya tenemos un modelo enfocado: reforzar
    if "detalle" in _norm(text) and focused:
        # solo pasamos esa opción
        options = [focused]
        ctx = {**context, "state": "detail", "options": options}
        return generate_reply(text, options, ctx)

    # 3) Seleccionar opciones SIN IA (para evitar inventos)
    options = pick_options(text, items, limit=2)

    # 4) Si pidió un modelo (toano/tunland) pero no encontramos match, aclarar con 1 pregunta corta
    wants_specific = any(w in _norm(text) for w in ["toano", "tuano", "tunland", "miler", "panel"])
    if wants_specific and not options:
        ctx = {**context, "state": "clarify"}
        return generate_reply(
            "El cliente escribió un modelo pero no lo encuentro exactamente. Pide aclaración corta.",
            [],
            ctx
        )

    # 5) Si saludo: NO mostrar inventario todavía, solo 1 pregunta
    if any(w in _norm(text) for w in ["hola", "qué tal", "que tal", "buenas", "buen día", "buen dia"]):
        ctx = {**context, "state": "greeting"}
        return generate_reply(text, [], ctx)

    # 6) Si tenemos opciones, las guardamos como foco (memoria)
    if options:
        context["focused_model"] = options[0]  # el primero como foco
        ctx = {**context, "state": "show_options", "options": options}
        return generate_reply(text, options, ctx)

    # 7) Si no hay opciones y no es saludo, pedir 1 dato (uso o presupuesto)
    ctx = {**context, "state": "no_match"}
    return generate_reply(text, [], ctx)

