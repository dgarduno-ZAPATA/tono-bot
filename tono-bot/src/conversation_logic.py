import re
from difflib import SequenceMatcher
from src.ai_reply import generate_reply


# -------- Helpers --------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()

def _get(it: dict, *keys, default=""):
    for k in keys:
        if k in it and it[k] not in (None, ""):
            return it[k]
    return default

def _dedup_key(it: dict) -> str:
    modelo = str(_get(it, "Modelo", "modelo", default="")).strip().lower()
    anio = str(_get(it, "Año", "Anio", "anio", default="")).strip()
    precio = str(_get(it, "Precio", "Precio Distribuidor", "precio", default="")).strip()
    return f"{modelo}|{anio}|{precio}"

def _make_label(it: dict) -> str:
    # Etiqueta corta para el cliente
    marca = str(_get(it, "Marca", "marca", default="")).strip()
    modelo = str(_get(it, "Modelo", "modelo", default="")).strip()
    anio = str(_get(it, "Año", "Anio", "anio", default="")).strip()
    precio = str(_get(it, "Precio", "Precio Distribuidor", "precio", default="")).strip()
    parts = [p for p in [marca, modelo, anio] if p]
    label = " ".join(parts) if parts else "Opción"
    if precio:
        label += f" - ${precio}"
    return label


def pick_options(user_text: str, items: list[dict], limit: int = 2) -> list[dict]:
    """
    Selecciona hasta 'limit' opciones del inventario basadas en similitud con el texto.
    Deduplica opciones idénticas para no repetir.
    """
    if not items:
        return []

    t = _norm(user_text)

    scored = []
    for it in items:
        marca = str(_get(it, "Marca", "marca", default="")).strip()
        modelo = str(_get(it, "Modelo", "modelo", default="")).strip()

        # si no hay modelo, lo ignoramos
        if not (marca or modelo):
            continue

        # score principal por marca+modelo
        score = _similar(t, f"{marca} {modelo}")
        scored.append((score, it))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Deduplicación + corte
    seen = set()
    chosen = []
    for score, it in scored:
        dk = _dedup_key(it)
        if dk in seen:
            continue
        seen.add(dk)
        chosen.append((score, it))
        if len(chosen) >= limit:
            break

    # Umbral: si es muy bajo, consideramos que no entendimos el modelo
    if not chosen:
        return []
    top_score = chosen[0][0]
    if top_score < 0.25:
        return []

    return [x[1] for x in chosen]


def _extract_option_choice(text: str) -> int | None:
    """
    Si el usuario dice 'opción 1', '1', 'la 2', etc., regresa índice 0 o 1.
    """
    t = _norm(text)
    # busca un 1 o 2 "solo"
    m = re.search(r"\b(1|2)\b", t)
    if not m:
        return None
    n = int(m.group(1))
    return n - 1


# -------- Main handler --------
def handle_message(message, inventory_service, state, context):
    items = inventory_service.items if hasattr(inventory_service, "items") else []
    text = (message or "").strip()
    t = _norm(text)

    context = context or {}
    state = state or "start"

    # Memoria
    focused = context.get("focused_model")
    last_options = context.get("last_options")  # lista de dicts (máx 2)

    # Detectores de intención rápida
    is_greeting = any(w in t for w in ["hola", "qué tal", "que tal", "buenas", "buen día", "buen dia", "buenas tardes", "buenas noches"])
    wants_detail = "detalle" in t or "más detalle" in t or "mas detalle" in t
    wants_photos = "foto" in t or "fotos" in t or "imagen" in t or "imagenes" in t
    wants_location = any(w in t for w in ["dónde", "donde", "ubicación", "ubicacion", "en dónde", "en donde", "quiero verla", "verla"])

    # 0) Si el usuario selecciona opción 1/2 y tenemos last_options
    choice = _extract_option_choice(text)
    if choice is not None and isinstance(last_options, list) and 0 <= choice < len(last_options):
        context["focused_model"] = last_options[choice]
        focused = context["focused_model"]

        # si pidió fotos, forzamos state fotos
        if wants_photos:
            ctx = {**context, "state": "photos", "options": [focused]}
            return generate_reply("El cliente eligió una opción y pide fotos. Confirma y ofrece enviarlas.", [focused], ctx)

        # si pidió detalle, forzamos detalle
        if wants_detail:
            ctx = {**context, "state": "detail", "options": [focused]}
            return generate_reply(text, [focused], ctx)

        # si pidió ubicación, responde ubicación (aunque no haya sucursal)
        if wants_location:
            ctx = {**context, "state": "location", "options": [focused]}
            return generate_reply("El cliente quiere verla. Pregunta en qué ciudad/sucursal y ofrece agendar cita.", [focused], ctx)

        # default: confirmar la elección y avanzar a cita
        ctx = {**context, "state": "selected", "options": [focused]}
        return generate_reply("El cliente eligió una opción. Confirma y pregunta si quiere agendar cita hoy o mañana.", [focused], ctx)

    # 1) Si saludo: NO listar inventario, solo 1 pregunta
    if is_greeting and state == "start":
        ctx = {**context, "state": "greeting"}
        return generate_reply(text, [], ctx)

    # 2) Si pide fotos pero no hay foco: pedir que elija opción 1/2
    if wants_photos and not focused:
        ctx = {**context, "state": "need_choice_for_photos"}
        return generate_reply("El cliente pide fotos pero no hay opción elegida. Pide que elija opción 1 o 2.", [], ctx)

    # 3) Si pide más detalle y ya hay foco: mantener hilo
    if wants_detail and focused:
        ctx = {**context, "state": "detail", "options": [focused]}
        return generate_reply(text, [focused], ctx)

    # 4) Si quiere verla / ubicación y hay foco: mantener hilo
    if wants_location and focused:
        ctx = {**context, "state": "location", "options": [focused]}
        return generate_reply("El cliente quiere verla. Pregunta en qué sucursal/ciudad y ofrece cita hoy o mañana.", [focused], ctx)

    # 5) Selección de opciones SIN IA (evita inventos)
    options = pick_options(text, items, limit=2)

    # 6) Si el usuario pidió un modelo específico que NO existe (ej. G9)
    # Detectamos tokens típicos de modelo y si no hay match, ofrecemos alternativas cercanas
    wants_model = any(w in t for w in ["toano", "tuano", "tunland", "miler", "panel", "g9", "g7", "e5", "g8"])
    if wants_model and not options:
        # Alternativas: toma las primeras 2 del inventario (dedup) como fallback
        # (si quieres, aquí luego filtramos por marca Tunland/Toano cuando tu inventario tenga marca/modelo consistente)
        fallback = []
        seen = set()
        for it in items:
            dk = _dedup_key(it)
            if dk in seen:
                continue
            seen.add(dk)
            fallback.append(it)
            if len(fallback) >= 2:
                break

        ctx = {**context, "state": "model_not_found", "requested_model": text, "options": fallback}
        return generate_reply("El cliente pidió un modelo que no está. Ofrece alternativas reales y pregunta preferencia.", fallback, ctx)

    # 7) Si encontramos opciones, guardar memoria y preguntar 1 cosa (opción 1 o 2)
    if options:
        context["focused_model"] = options[0]
        context["last_options"] = options  # para que luego “1/2” funcione
        # le pasamos labels por si quieres que la IA lo formatee mejor
        labels = [_make_label(o) for o in options]
        ctx = {**context, "state": "show_options", "options": options, "option_labels": labels}
        return generate_reply(text, options, ctx)

    # 8) Si no hay match: pedir 1 dato útil (uso/presupuesto/modelo correcto)
    ctx = {**context, "state": "no_match"}
    return generate_reply(text, [], ctx)

