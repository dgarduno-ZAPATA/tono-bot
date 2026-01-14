import json
import re
from src.ai_reply import generate_reply

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _label(it: dict) -> str:
    marca = (it.get("Marca") or "").strip()
    modelo = (it.get("Modelo") or "").strip()
    anio = str(it.get("Año") or "").strip()
    precio = str(it.get("Precio") or it.get("Precio Distribuidor") or "").strip()
    base = " ".join([x for x in [marca, modelo, anio] if x])
    if precio:
        base += f" – ${precio}"
    return base if base else "Opción"

def _safe_reply(options: list[dict], state: str) -> str:
    # Mensajes seguros que nunca inventan
    if state == "greeting":
        return "¡Hola! ¿Buscas auto, pickup/camioneta o camión?"
    if not options:
        return "¿Qué modelo te interesa o qué buscas (auto, pickup/camioneta o camión)?"
    if state in ("show_options", "model_not_found"):
        lines = []
        for i, o in enumerate(options[:2], start=1):
            lines.append(f"{i}) {_label(o)}")
        return "Tengo estas opciones:\n" + "\n".join(lines) + "\n¿Cuál te interesa más, 1 o 2?"
    if state == "detail":
        return f"Perfecto. {_label(options[0])}\n¿Quieres verla hoy o mañana?"
    return "Perfecto. ¿Te interesa agendar cita hoy o mañana?"

def handle_message(message, inventory_service, state, context):
    inv = inventory_service.items if hasattr(inventory_service, "items") else []
    context = context or {}
    state = state or "start"

    user_text = (message or "").strip()
    t = _norm(user_text)

    # Estado saludo simple
    if any(w in t for w in ["hola", "buenas", "buen día", "buen dia", "buenas tardes", "buenas noches", "que tal", "qué tal"]):
        new_state = "greeting"
        return {"reply": _safe_reply([], "greeting"), "new_state": new_state, "context": context}

    # Llamada a IA (devuelve dict)
    result = generate_reply(user_text, inv, {"state": state, **context})

    # Asegurar forma
    if not isinstance(result, dict):
        # si algo raro regresa texto, lo usamos pero sin romper
        return {"reply": str(result), "new_state": state, "context": context}

    reply = (result.get("reply") or "").strip()
    idxs = result.get("selected_indexes") or []
    new_state = result.get("new_state") or state

    # Validar índices y construir opciones reales
    options = []
    for i in idxs[:2]:
        if isinstance(i, int) and 0 <= i < len(inv):
            options.append(inv[i])

    # Guardar memoria de opciones y foco
    if options:
        context["last_options"] = options
        context["focused_model"] = options[0]

    # Guardrail anti-inventos:
    # Si el reply menciona cosas que no están en las opciones elegidas, mejor usamos mensaje seguro.
    allowed_tokens = set()
    for o in options:
        allowed_tokens.add(_norm(o.get("Modelo", "")))
        allowed_tokens.add(_norm(o.get("Marca", "")))

    # si no hay opciones, no permitimos que mencione modelos específicos como si existieran
    mentions_modelish = any(w in _norm(reply) for w in ["g9", "miler", "panel", "at", "4x4"]) and not options
    if mentions_modelish:
        reply = _safe_reply([], "no_match")
        new_state = "no_match"

    # si hay opciones, pero el reply menciona un modelo que no coincide con ninguna opción, usamos plantilla segura
    if options:
        ok = False
        repn = _norm(reply)
        for o in options:
            if _norm(o.get("Modelo", "")) and _norm(o.get("Modelo", "")) in repn:
                ok = True
        # si no mencionó ningún modelo real, no pasa nada; pero si mencionó "g9/miler" y no está en opciones, cortamos
        bad = any(w in repn for w in ["g9", "miler"]) and not any(w in repn for w in [ _norm(o.get("Modelo","")) for o in options if o.get("Modelo") ])
        if bad:
            reply = _safe_reply(options, "show_options")
            new_state = "show_options"

    # Si reply viene vacío, mandamos seguro
    if not reply:
        reply = _safe_reply(options, new_state)

    return {"reply": reply, "new_state": new_state, "context": context}
