import re
from src.ai_reply import generate_reply

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _label(it: dict) -> str:
    marca = (it.get("Marca") or "").strip()
    modelo = (it.get("Modelo") or "").strip()
    anio = str(it.get("Año") or "").strip()
    precio = str(it.get("Precio") or it.get("Precio Distribuidor") or "").strip()
    base = " ".join([x for x in [marca, modelo, anio] if x]).strip()
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

def _extract_option_choice(text: str) -> int | None:
    t = _norm(text)
    m = re.search(r"\b(1|2)\b", t)
    if not m:
        return None
    return int(m.group(1)) - 1

def handle_message(message, inventory_service, state, context):
    inv = inventory_service.items if hasattr(inventory_service, "items") else []
    context = context or {}
    state = state or "start"

    user_text = (message or "").strip()
    t = _norm(user_text)

    # 0) Si el usuario responde "1" o "2" y tenemos last_options, enfocamos esa opción
    choice = _extract_option_choice(user_text)
    if choice is not None and isinstance(context.get("last_options"), list):
        last_options = context["last_options"]
        if 0 <= choice < len(last_options):
            context["focused_model"] = last_options[choice]
            focused = context["focused_model"]
            return {
                "reply": _safe_reply([focused], "detail"),
                "new_state": "detail",
                "context": context
            }

    # 1) Saludo: corto, sin inventario
    if any(w in t for w in ["hola", "buenas", "buen día", "buen dia", "buenas tardes", "buenas noches", "que tal", "qué tal"]):
        return {"reply": _safe_reply([], "greeting"), "new_state": "greeting", "context": context}

    # 2) Llamada a IA: devuelve dict {reply, selected_indexes, new_state}
    result = generate_reply(user_text, inv, {"state": state, **context})

    # 3) Si por cualquier razón no es dict, NUNCA regresamos ese texto (evita JSON)
    if not isinstance(result, dict):
        return {"reply": _safe_reply([], "no_match"), "new_state": "no_match", "context": context}

    reply = (result.get("reply") or "").strip()
    idxs = result.get("selected_indexes") or []
    new_state = result.get("new_state") or state

    # 4) Validar índices (solo opciones reales)
    options = []
    for i in idxs[:2]:
        if isinstance(i, int) and 0 <= i < len(inv):
            options.append(inv[i])

    # 5) Guardar memoria para continuidad (fotos mañana)
    if options:
        context["last_options"] = options
        context["focused_model"] = options[0]

    # 6) Guardrail anti-inventos: si reply viene sospechoso, usamos seguro
    repn = _norm(reply)

    # Si no hay opciones, no permitimos mencionar modelos "como si existieran"
    if not options and any(w in repn for w in ["g9", "miler", "panel", "4x4", "at", "diesel", "azul", "negro"]):
        reply = _safe_reply([], "no_match")
        new_state = "no_match"

    # Si hay opciones pero reply menciona "g9/miler" y ninguna opción lo contiene -> seguro
    if options:
        modelos_reales = [_norm(o.get("Modelo", "")) for o in options if o.get("Modelo")]
        if any(w in repn for w in ["g9", "miler"]) and not any(m and m in repn for m in modelos_reales):
            reply = _safe_reply(options, "show_options")
            new_state = "show_options"

    # Si reply vacío, seguro
    if not reply:
        # si hay opciones y new_state suena a show, usamos show_options
        if options:
            reply = _safe_reply(options, "show_options")
            new_state = "show_options"
        else:
            reply = _safe_reply([], "no_match")
            new_state = "no_match"

    # 7) SIEMPRE regresamos dict para main.py
    return {"reply": reply, "new_state": str(new_state), "context": context}
