import os
import re
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import pytz
from openai import AsyncOpenAI, APITimeoutError, RateLimitError, APIStatusError

logger = logging.getLogger(__name__)

# ============================================================
# CONFIG
# ============================================================
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ============================================================
# TIME (CDMX)
# ============================================================
def get_mexico_time() -> Tuple[datetime, str]:
    """Returns current datetime in Mexico City timezone and a readable string."""
    try:
        tz = pytz.timezone("America/Mexico_City")
        now = datetime.now(tz)
        return now, now.strftime("%A %I:%M %p")
    except Exception as e:
        logger.error(f"Timezone error: {e}")
        now = datetime.now()
        return now, now.strftime("%A %I:%M %p")


# ============================================================
# PROMPT (IMPORTANT: JSON example uses DOUBLE BRACES {{ }})
# ============================================================
SYSTEM_PROMPT = """
Eres "Adrian Jimenez", asesor de 'Tractos y Max'.

OBJETIVO: Tu trabajo NO es vender. Tu trabajo es DESTRABAR.
Elimina barreras para que el cliente quiera venir. Responde directo y breve.

DATOS CLAVE:
- Ubicación: Tlalnepantla, Edo Mex (Camiones del Valle Tlalnepantla).
- Horario: Lunes a Viernes 9-6 PM. Sábados 9-2 PM. DOMINGOS CERRADO.
- FECHA ACTUAL: {current_date_str}
- HORA ACTUAL: {current_time_str}
- CLIENTE: {user_name_context}
- TURNO: {turn_number}

INFORMACIÓN FOTON:
- Tractos y Max es DISTRIBUIDOR AUTORIZADO FOTON.
- FACTURA ORIGINAL de FOTON (no reventa, no intermediario).
- GARANTÍA: De fábrica FOTON, válida en todo México.
- SERVICIO: El cliente puede hacer mantenimiento en cualquier distribuidor FOTON autorizado del país sin perder garantía.

DOCUMENTACIÓN PARA COMPRA:
- CONTADO: INE vigente + comprobante de domicilio. Si quiere factura a su RFC, también Constancia de Situación Fiscal.
- CRÉDITO: NO des lista de documentos. Di: "Un asesor te envía los requisitos."

REGLAS OBLIGATORIAS:

1) IDENTIDAD:
- Si preguntan "¿con quién hablo?" o "¿quién eres?": PRIMERO di "Soy Adrian Jimenez, asesor de Tractos y Max."
- NUNCA pidas el nombre del cliente ANTES de dar el tuyo.
- SOLO saluda "Hola" en turno 1.

2) DISCLAIMER DE INTERMEDIARIO (PROTOCOLO DE TRANSPARENCIA):
- CUÁNDO: Turno 2 o 3 únicamente, si el cliente ya interactuó.
- DISPARADORES OBLIGATORIOS (menciona el rol si preguntan por): Precio, Ubicación, Cita/visita, Detalles de pago, Proceso de compra, o "¿Ustedes venden?"
- PROHIBIDO: En turno 1 (muy pronto). Repetirlo si ya lo dijiste (revisa HISTORIAL).
- REGLA DEL SÁNDWICH (siempre en este orden):
  1. Responder la duda del cliente
  2. Aclarar rol de intermediario de forma natural
  3. Proponer siguiente paso (CTA)
- VARIANTES (rota para no sonar robot):
  * "Te comento que nosotros funcionamos como enlace e intermediarios en este proceso, con gusto te apoyo coordinando..."
  * "Para tu tranquilidad, nosotros actuamos como intermediarios y facilitadores en este proceso..."
  * "Como somos el enlace intermediario, me encargo de conectarte con la persona responsable..."
  * "Es importante mencionar que participamos únicamente como intermediarios en esta gestión..."
- EJEMPLO COMPLETO: "Claro, la Tunland G9 está en $720,000. Te comento que nosotros funcionamos como intermediarios en este proceso, con gusto te apoyo coordinando una visita. ¿Qué día te funcionaría?"
- OBJETIVO: Informar con naturalidad, no generar alarma. Sonar útil y orientado a facilitar.

3) PREGUNTA "¿ES IA?" o "¿ME RESPONDE UN BOT?":
- Responde: "Te atiendo por el canal digital de Tractos y Max. Si prefieres, te marco y lo vemos por llamada."
- NO digas "soy IA", "soy bot", "soy asistente virtual".

4) CANDADO DE MODELO (CRÍTICO):
- Si ya se detectó un modelo en la conversación (ej. Tunland G9 2025), NO cambies a otro modelo sin que el cliente lo pida explícitamente.
- Si el cliente menciona otro modelo, pregunta: "¿Te refieres a la [modelo nuevo] o seguimos con la [modelo anterior]?"
- NUNCA mezcles información de dos modelos diferentes en la misma respuesta.

5) RESPUESTAS CORTAS:
- MÁXIMO 2 oraciones por mensaje.
- NO des explicaciones largas ni definiciones.
- Si no sabes algo: "Eso lo confirmo y te aviso."

6) ANTI-REPETICIÓN:
- NUNCA preguntes algo que ya sabes.
- Revisa HISTORIAL antes de responder.

7) RESPONDE SOLO LO QUE PREGUNTAN:
- Precio → Da el precio del modelo en conversación.
- Fotos → "Claro, aquí tienes."
- Ubicación → "Estamos en Tlalnepantla, Edo Mex: https://maps.app.goo.gl/v9KigGY3QVAxqwV17" (NUNCA uses formato [texto](url), solo el URL directo)
- Garantía/Servicio → "Puede hacer servicio en cualquier distribuidor FOTON autorizado sin perder garantía."
- "Muy bien" / "Ok" → "Perfecto." y espera.

8) FINANCIAMIENTO (REGLAS DE ORO):
- PUEDES dar información de corridas financieras BASE cuando pregunten.
- DATOS BASE que SÍ puedes dar:
  * Enganche mínimo: SIEMPRE es 20% del valor factura.
  * Plazo base: SIEMPRE es 48 meses (4 años).
  * Mensualidad estimada: USA los datos de CORRIDAS FINANCIERAS abajo.
  * Las mensualidades YA INCLUYEN intereses, IVA de intereses y seguros.
- OBLIGATORIO: SIEMPRE que menciones un número (enganche, mensualidad, precio financiado), di que es ILUSTRATIVO.
  * Ejemplo: "El enganche mínimo sería de $90,000 y la mensualidad aproximada de $12,396, esto es ilustrativo."
  * Ejemplo: "Con enganche del 20% ($144,000) quedarían mensualidades de aproximadamente $19,291, como referencia ilustrativa."
- ESCALAR A ASESOR cuando pidan:
  * Más enganche (mayor al 20%) → "Sí es posible, un asesor te contacta para personalizar."
  * Otro plazo (diferente a 48 meses) → "El plazo base es 48 meses. Para ajustarlo, un asesor te contacta."
  * Bajar intereses / cambiar tasa → "Un asesor te contacta para ver opciones."
  * Quitar seguros / otra personalización → "Un asesor te contacta."
- Para ESCALAR pide: Nombre, Teléfono (si no lo tienes), Ciudad, Modelo de interés.

9) MODO ESPERA:
- Si dice "déjame ver", "ocupado", etc: "Sin problema, aquí quedo pendiente." y PARA.

10) FOTOS:
- Si piden fotos: "Claro, aquí tienes." (el sistema las adjunta).

11) PDFs (FICHA TÉCNICA Y CORRIDA FINANCIERA):
- Si piden "ficha técnica", "especificaciones", "specs": responde "Claro, te comparto la ficha técnica en PDF." (el sistema adjunta el PDF).
- Si piden "corrida", "simulación de financiamiento", "tabla de pagos": responde "Listo, te comparto la simulación de financiamiento en PDF. Es ilustrativa e incluye intereses." (el sistema adjunta el PDF).
- Si NO hay modelo detectado en la conversación, pregunta primero: "¿De cuál unidad te interesa? Tenemos Toano Panel, Tunland G9, Tunland E5, EST-A y Miller."
- Si NO tenemos el PDF de ese modelo, responde: "Por el momento no tengo ese documento en PDF, pero un asesor te lo puede compartir."

12) CITAS:
- DOMINGOS CERRADO. Si propone domingo: "Los domingos no abrimos. ¿Te parece el lunes o sábado?"
- ANTI-INSISTENCIA: NO termines cada mensaje con "¿Te gustaría agendar una cita?"
- Solo menciona la cita cuando sea NATURAL: después de dar precio, después de 3-4 intercambios, o si el cliente pregunta cuándo puede ir.
- Si ya sugeriste cita y el cliente NO respondió sobre eso, NO insistas. Espera a que él pregunte.

13) LEAD (JSON):
- SOLO genera JSON si hay: NOMBRE + MODELO + CITA CONFIRMADA.
```json
{{
  "lead_event": {{
    "nombre": "Juan Perez",
    "interes": "Foton Tunland G9 2025",
    "cita": "Lunes 10 AM",
    "pago": "Contado"
  }}
}}
```

14) PROHIBIDO:
- Emojis
- Explicaciones largas
- Inventar información
- Calcular financiamiento
- Pedir nombre antes de dar el tuyo
- Cambiar de modelo sin confirmación del cliente
- Formato markdown para links (NO uses [texto](url), WhatsApp no lo soporta)
""".strip()


# ============================================================
# FINANCING DATA
# ============================================================
_FINANCING_DATA: Optional[Dict[str, Any]] = None


def _load_financing_data() -> Dict[str, Any]:
    """Load financing data from JSON file (cached)."""
    global _FINANCING_DATA
    if _FINANCING_DATA is not None:
        return _FINANCING_DATA

    financing_path = os.path.join(os.path.dirname(__file__), "..", "data", "financing.json")
    try:
        with open(financing_path, "r", encoding="utf-8") as f:
            _FINANCING_DATA = json.load(f)
            logger.info(f"✅ Financing data loaded: {len(_FINANCING_DATA)} models")
    except FileNotFoundError:
        logger.warning(f"⚠️ Financing file not found: {financing_path}")
        _FINANCING_DATA = {}
    except json.JSONDecodeError as e:
        logger.error(f"❌ Error parsing financing JSON: {e}")
        _FINANCING_DATA = {}

    return _FINANCING_DATA


def _build_financing_text() -> str:
    """Build financing info text for GPT context."""
    data = _load_financing_data()
    if not data:
        return "Corridas de financiamiento no disponibles."

    lines = ["CORRIDAS FINANCIERAS (Banorte - Ilustrativas):"]
    lines.append("Enganche mínimo: 20% | Plazo base: 48 meses | Mensualidades YA incluyen intereses y seguros\n")

    for key, info in data.items():
        nombre = info.get("nombre", "")
        anio = info.get("anio", "")
        transmision = info.get("transmision", "")
        valor = info.get("valor_factura", 0)
        enganche = info.get("enganche_min", 0)
        mensualidad = info.get("pago_mensual_total_mes_1", 0)
        tasa = info.get("tasa_anual_pct", 0)
        cat = info.get("cat_sin_iva_pct", 0)

        trans_text = f" ({transmision})" if transmision else ""
        lines.append(
            f"- {nombre} {anio}{trans_text}: "
            f"Factura ${valor:,.0f} | "
            f"Enganche 20% = ${enganche:,.0f} | "
            f"Mensualidad ~${mensualidad:,.2f} | "
            f"Tasa {tasa}% | CAT {cat}%"
        )

    return "\n".join(lines)


def _detect_pdf_request(user_message: str, last_interest: str, context: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """
    Detecta si el usuario pide un PDF (ficha técnica o corrida).
    Retorna dict con: tipo, pdf_url, filename, mensaje_previo
    O None si no pide PDF.

    Ahora con soporte de contexto para:
    - Typos comunes ("fiche", "fixa", "corrda")
    - Peticiones genéricas ("pásamela", "mándamela") si hubo PDF previo
    """
    msg = (user_message or "").lower()
    context = context or {}

    # Detectar tipo de PDF solicitado (con typos comunes)
    ficha_keywords = [
        "ficha", "fiche", "fixa", "ficah",  # typos
        "ficha tecnica", "ficha técnica",
        "especificaciones", "specs", "caracteristicas", "características",
        "hoja tecnica", "hoja técnica", "datos tecnicos", "datos técnicos"
    ]
    corrida_keywords = [
        "corrida", "corrda", "corida",  # typos
        "simulacion", "simulación", "simulacion de",
        "financiamiento", "tabla de pagos",
        "mensualidades pdf", "pagos mensuales",
        "plan de pagos", "cuotas"
    ]

    # Keywords genéricos que continúan un PDF previo
    generic_send_keywords = [
        "pasame", "pásame", "pasala", "pásala", "pasamela", "pásamela",
        "mandame", "mándame", "mandala", "mándala", "mandamela", "mándamela",
        "enviame", "envíame", "enviala", "envíala", "enviamela", "envíamela",
        "comparteme", "compárteme", "compartela", "compártela",
        "dame", "dámela", "la quiero", "si la quiero", "sí la quiero"
    ]

    pdf_type = None
    if any(k in msg for k in ficha_keywords):
        pdf_type = "ficha"
        logger.debug(f"📄 Keyword de ficha detectado en: '{msg}'")
    elif any(k in msg for k in corrida_keywords):
        pdf_type = "corrida"
        logger.debug(f"📄 Keyword de corrida detectado en: '{msg}'")

    # Si no hay keyword explícito, verificar si hay petición genérica + contexto previo
    if not pdf_type:
        last_pdf_type = context.get("last_pdf_request_type")
        if last_pdf_type and any(k in msg for k in generic_send_keywords):
            pdf_type = last_pdf_type
            logger.info(f"📄 Petición genérica '{msg}' continuando PDF previo: {pdf_type}")

    if not pdf_type:
        return None

    # Necesitamos un modelo detectado
    if not last_interest:
        logger.info(f"📄 PDF {pdf_type} solicitado pero no hay last_interest")
        return {"tipo": pdf_type, "sin_modelo": True}

    # Buscar el modelo en los datos de financiamiento
    data = _load_financing_data()
    if not data:
        logger.warning(f"📄 PDF {pdf_type} solicitado pero no hay datos de financiamiento")
        return {"tipo": pdf_type, "sin_datos": True}

    # Normalizar el interés para buscar
    interest_norm = last_interest.lower().replace("foton", "").replace("diesel", "").replace("4x4", "").strip()
    logger.info(f"📄 Buscando modelo para PDF: last_interest='{last_interest}' -> normalizado='{interest_norm}'")

    # Buscar coincidencia
    matched_key = None
    matched_info = None
    best_score = 0
    best_year = 0

    for key, info in data.items():
        nombre = info.get("nombre", "").lower()
        anio = int(info.get("anio", 0))

        # Tokens del modelo (únicos, sin duplicados)
        key_tokens = set(key.lower().replace("_", " ").split())
        nombre_tokens = set(nombre.split())
        all_tokens = key_tokens.union(nombre_tokens)

        # Verificar si hay coincidencia (solo tokens de 2+ caracteres, excluyendo "foton")
        score = 0
        matched_tokens = []
        for token in all_tokens:
            if len(token) >= 2 and token != "foton" and token in interest_norm:
                score += 1
                matched_tokens.append(token)

        # También verificar año - bonus alto si hay coincidencia exacta
        year_str = str(anio)
        if year_str in interest_norm or year_str in last_interest:
            score += 3  # Bonus alto por año exacto
            matched_tokens.append(f"año:{anio}")

        if score > 0:
            logger.debug(f"📄 Candidato '{key}': score={score}, año={anio}, tokens={matched_tokens}")

        # Aceptar si score >= 2
        # Preferir: mayor score, o mismo score pero año más reciente
        if score >= 2:
            is_better = (
                matched_key is None or
                score > best_score or
                (score == best_score and anio > best_year)
            )
            if is_better:
                matched_key = key
                matched_info = info.copy()
                matched_info["_score"] = score
                best_score = score
                best_year = anio

    if not matched_info:
        logger.info(f"📄 No se encontró modelo para '{interest_norm}' en financiamiento")
        return {"tipo": pdf_type, "sin_modelo": True}

    logger.info(f"📄 Modelo matched: '{matched_key}' (score={best_score}, año={best_year}) para '{last_interest}'")

    # Obtener URL del PDF
    if pdf_type == "ficha":
        pdf_url = matched_info.get("pdf_ficha_tecnica")
        if not pdf_url:
            return {"tipo": pdf_type, "sin_pdf": True, "modelo": matched_info.get("nombre", "")}
        filename = f"Ficha_Tecnica_{matched_info.get('nombre', 'Foton').replace(' ', '_')}_{matched_info.get('anio', '')}.pdf"
        mensaje = "Claro, te comparto la ficha tecnica en PDF."
    else:
        pdf_url = matched_info.get("pdf_corrida")
        if not pdf_url:
            return {"tipo": pdf_type, "sin_pdf": True, "modelo": matched_info.get("nombre", "")}
        filename = f"Corrida_Financiamiento_{matched_info.get('nombre', 'Foton').replace(' ', '_')}_{matched_info.get('anio', '')}.pdf"
        mensaje = "Listo, te comparto la simulacion de financiamiento en PDF. Es ilustrativa e incluye intereses."

    return {
        "tipo": pdf_type,
        "pdf_url": pdf_url,
        "filename": filename,
        "mensaje": mensaje,
        "modelo": f"{matched_info.get('nombre', '')} {matched_info.get('anio', '')}"
    }


# ============================================================
# INVENTORY HELPERS
# ============================================================
def _safe_get(item: Dict[str, Any], keys: List[str], default: str = "") -> str:
    """Return first non-empty string for given keys."""
    for k in keys:
        v = item.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def _format_price(precio: str, moneda: str, iva: str) -> str:
    """Precio limpio: '$499,000 MXN IVA incluido'."""
    try:
        num = float(precio.replace(",", "").replace(" ", ""))
        formatted = f"${num:,.0f}"
    except (ValueError, AttributeError):
        formatted = f"${precio}" if precio else "Consultar"
    cur = moneda if moneda else "MXN"
    iva_txt = " IVA incluido" if iva and iva.upper() == "TRUE" else ""
    return f"{formatted} {cur}{iva_txt}"


def _summarize_motor(raw: str) -> str:
    """Extrae lo útil del bloque MOTOR y lo resume en una frase."""
    if not raw:
        return ""
    lines = [l.strip() for l in raw.replace("\r", "").split("\n") if l.strip()]
    parts = {}
    for line in lines:
        if ":" in line:
            k, v = line.split(":", 1)
            parts[k.strip().lower()] = v.strip()
        else:
            parts.setdefault("extra", line)

    brand = parts.get("marca", "")
    cil = parts.get("cilindrada", "")
    potencia = parts.get("potencia", "")

    pieces = []
    if brand:
        pieces.append(brand)
    if cil:
        pieces.append(cil)
    if potencia:
        pieces.append(potencia)
    return ", ".join(pieces) if pieces else raw.split("\n")[0][:80]


def _summarize_capacity(raw: str) -> str:
    """'Carga maxima: 900 kg' → '900 kg'. 'Carga sobre chasis 3,700 kg' → '3.7 ton'."""
    if not raw:
        return ""
    m = re.search(r"([\d,\.]+)\s*kg", raw, re.IGNORECASE)
    if m:
        try:
            kg = float(m.group(1).replace(",", ""))
            if kg >= 1000:
                return f"{kg/1000:.1f} toneladas"
            return f"{kg:.0f} kg"
        except ValueError:
            pass
    if "tonelada" in raw.lower():
        return raw.strip()
    return raw.split("\n")[0].strip()[:60]


def _normalize_fuel(raw: str) -> str:
    """Normaliza combustible a 'Gasolina' o 'Diésel'."""
    if not raw:
        return ""
    low = raw.lower()
    if "diesel" in low or "diésel" in low:
        return "Diésel"
    if "gasolina" in low:
        return "Gasolina"
    return raw.strip()[:30]


def _build_inventory_text(inventory_service) -> str:
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return "Inventario no disponible."

    lines: List[str] = []
    for item in items:
        marca = _safe_get(item, ["Marca", "marca"], default="Foton")
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"], default="(sin modelo)")
        anio = _safe_get(item, ["Anio", "Año", "anio"], default="")
        color = _safe_get(item, ["Color", "color"], default="")
        segmento = _safe_get(item, ["segmento", "descripcion_corta"], default="")
        precio = _safe_get(item, ["Precio", "precio"], default="N/D")
        moneda = _safe_get(item, ["moneda"], default="MXN")
        iva = _safe_get(item, ["iva_incluido"], default="")
        combustible = _normalize_fuel(_safe_get(item, ["COMBUSTIBLE", "combustible"]))
        motor_raw = _safe_get(item, ["MOTOR", "motor"])
        motor = _summarize_motor(motor_raw)
        capacidad = _summarize_capacity(_safe_get(item, ["CAPACIDAD DE CARGA"]))
        llantas = _safe_get(item, ["LLANTAS"], default="")
        garantia = _safe_get(item, ["garantia_texto"], default="")
        ubicacion = _safe_get(item, ["ubicacion"], default="")
        financiamiento = _safe_get(item, ["Financiamiento"], default="")

        # Línea principal: Modelo + Precio
        price_str = _format_price(precio, moneda, iva)
        info = f"- {marca} {modelo} {anio}"
        if color:
            info += f" ({color})"
        info += f": {price_str}"
        if segmento:
            info += f" [{segmento}]"

        # Specs resumidas (solo lo útil para vender)
        specs = []
        if combustible:
            specs.append(combustible)
        if motor:
            specs.append(f"Motor: {motor}")
        if capacidad:
            specs.append(f"Carga: {capacidad}")
        if llantas:
            # Extraer solo la medida, sin repetir combustible
            llanta_clean = llantas.split("/")[0].strip() + "/" + llantas.split("/")[1].strip() if "/" in llantas else llantas
            m_llanta = re.search(r"\d{3}/\d{2,3}", llantas)
            if m_llanta:
                specs.append(f"Llantas: {m_llanta.group()}")
        if specs:
            info += " | " + ", ".join(specs)

        # Datos comerciales (una línea, sin ruido)
        extras = []
        if garantia:
            extras.append(f"Garantía: {garantia}")
        if financiamiento and financiamiento.upper() not in ("FALSE", "NO", "0", ""):
            extras.append("Crédito disponible")
        if ubicacion:
            extras.append(f"Ubic: {ubicacion}")
        if extras:
            info += " | " + ", ".join(extras)

        lines.append(info)

    return "\n".join(lines)


def _extract_photos_from_item(item: Dict[str, Any]) -> List[str]:
    raw = _safe_get(item, ["photos", "photo", "foto", "imagen", "imagenes", "fotos"])
    if not raw:
        return []
    return [u.strip() for u in raw.split("|") if u.strip().startswith("http")]


# ============================================================
# NAME / PAYMENT / APPOINTMENT EXTRACTION
# ============================================================
def _extract_name_from_text(text: str) -> Optional[str]:
    """Extract probable customer name (conservative)."""
    t = (text or "").strip()
    if not t:
        return None

    patterns = [
        r"\bme llamo\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bsoy\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bmi nombre es\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,3})\b",
        r"\bcon\s+([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+(?:\s+[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+){0,2})\b",
    ]

    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            bad = {"aqui", "aquí", "nadie", "yo", "el", "ella", "amigo", "desconocido", "cliente", "usuario", "quien", "quién"}
            if name.lower() in bad:
                return None
            return " ".join(w.capitalize() for w in name.split())

    return None


def _extract_payment_from_text(text: str) -> Optional[str]:
    msg = (text or "").lower()
    if any(k in msg for k in ["contado", "cash", "de contado"]):
        return "Contado"
    if any(k in msg for k in ["crédito", "credito", "financiamiento", "financiación"]):
        return "Crédito"
    return None


def _normalize_spanish(text: str) -> str:
    return (
        (text or "")
        .lower()
        .replace("miller", "miler")
        .replace("vanesa", "toano")
        .replace("la e5", "tunland e5")
    )


def _extract_interest_from_messages(user_message: str, reply: str, inventory_service) -> Optional[str]:
    """Infer model interest by matching inventory model tokens in user message or bot reply."""
    items = getattr(inventory_service, "items", None) or []
    if not items:
        return None

    msg_norm = _normalize_spanish(user_message)
    rep_norm = _normalize_spanish(reply)

    best: Optional[str] = None
    best_score = 0

    for item in items:
        modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
        if not modelo:
            continue

        modelo_norm = _normalize_spanish(modelo)
        # CAMBIO: Permitir tokens de 2 caracteres para detectar G9, E5, G7, etc.
        tokens = [t for t in modelo_norm.split() if len(t) >= 2 and t not in {"foton", "camion", "camión"}]
        if not tokens:
            continue

        score = 0
        for tok in tokens:
            if tok in msg_norm:
                score += 2
            if tok in rep_norm:
                score += 1

        if score > best_score:
            best_score = score
            best = modelo

    if best_score >= 2:
        return best

    return None


def _extract_appointment_from_text(text: str) -> Optional[str]:
    """Basic Spanish appointment extractor for day/time."""
    t = (text or "").strip().lower()
    if not t:
        return None

    day: Optional[str] = None
    if "mañana" in t:
        day = "Mañana"
    else:
        days = ["lunes", "martes", "miércoles", "miercoles", "jueves", "viernes", "sábado", "sabado", "domingo"]
        for d in days:
            if d in t:
                day = d.capitalize().replace("Miercoles", "Miércoles").replace("Sabado", "Sábado")
                break

    time_str: Optional[str] = None

    # "medio dia" o "mediodía"
    if "medio dia" in t or "mediodía" in t or "medio día" in t:
        time_str = "12:00"

    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*y\s*media\b", t)
        if m:
            h = int(m.group(1))
            time_str = f"{h}:30"

    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", t)
        if m:
            h = int(m.group(1))
            mm = int(m.group(2))
            if 0 <= h <= 23 and 0 <= mm <= 59:
                time_str = f"{h}:{mm:02d}"

    if not time_str:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
        if m:
            h = int(m.group(1))
            mer = m.group(2)
            if 1 <= h <= 12:
                hh = h % 12
                if mer == "pm":
                    hh += 12
                time_str = f"{hh}:00"

    if not time_str:
        if "en la tarde" in t or "por la tarde" in t:
            time_str = "(tarde)"
        elif "en la mañana" in t or "por la mañana" in t:
            time_str = "(mañana)"
        elif "en la noche" in t or "por la noche" in t:
            time_str = "(noche)"

    def _pretty_time_24_to_12(h24: int, mm: str) -> str:
        if h24 == 0:
            return f"12:{mm} AM"
        if 1 <= h24 <= 11:
            return f"{h24}:{mm} AM"
        if h24 == 12:
            return f"12:{mm} PM"
        return f"{h24 - 12}:{mm} PM"

    if day and time_str:
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            h24 = int(time_str.split(":")[0])
            mm = time_str.split(":")[1]
            return f"{day} {_pretty_time_24_to_12(h24, mm)}"
        return f"{day} {time_str}"

    if day and not time_str:
        return day

    if time_str and not day:
        if re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            h24 = int(time_str.split(":")[0])
            mm = time_str.split(":")[1]
            return _pretty_time_24_to_12(h24, mm)
        return time_str

    return None


def _message_confirms_appointment(text: str) -> bool:
    """
    Detecta si el mensaje es una confirmación de cita.
    Solo coincidencias exactas para evitar falsos positivos.
    """
    t = (text or "").strip().lower()
    if not t:
        return False

    confirmations = [
        "vale", "ok", "okey", "si", "sí", "listo", "perfecto",
        "nos vemos", "ahí nos vemos", "mañana nos vemos",
        "de acuerdo", "confirmo", "gracias", "está bien",
        "entendido", "excelente", "claro", "bien", "sale"
    ]

    return t in confirmations


# ============================================================
# PHOTOS LOGIC (🔥 CON MEMORIA DE ÍNDICE)
# ============================================================
def _pick_media_urls(
    user_message: str,
    reply: str,
    inventory_service,
    context: Dict[str, Any],
) -> List[str]:
    """
    Devuelve lista de URLs de fotos según el modelo detectado.
    Ahora con MEMORIA: guarda en context['photo_index'] para saber cuál foto va.
    """
    msg = _normalize_spanish(user_message)

    # 1) Si piden ubicación, no mandar fotos
    gps_keywords = ["ubicacion", "ubicación", "donde estan", "dónde están", "direccion", "dirección", "mapa", "donde se ubican"]
    if any(k in msg for k in gps_keywords):
        return []

    items = getattr(inventory_service, "items", None) or []
    if not items:
        return []

    # 2) Verificar si piden fotos EXPLÍCITAMENTE
    # Keywords que SIEMPRE indican petición de fotos
    explicit_photo_keywords = [
        "foto",
        "fotos",
        "imagen",
        "imagenes",
        "imágenes",
        "ver fotos",
        "ver imágenes",
        "ver la foto",
        "ver las fotos",
        "enseñame foto",
        "enséñame foto",
        "muestrame foto",
        "muéstrame foto",
        "mandame foto",
        "mándame foto",
    ]

    # Keywords que SOLO funcionan si ya hay contexto de fotos (photo_model existe)
    # Evita mandar fotos cuando dicen "otra cosa", "más información", etc.
    context_photo_keywords = ["otra foto", "mas fotos", "más fotos", "siguiente foto", "otra imagen"]

    current_photo_model = (context.get("photo_model") or "").strip()

    explicit_request = any(k in msg for k in explicit_photo_keywords)
    context_request = current_photo_model and any(k in msg for k in context_photo_keywords)

    if not explicit_request and not context_request:
        return []

    # 3) Recuperar memoria del contexto
    last_interest = (context.get("last_interest") or "").strip()
    current_photo_model = (context.get("photo_model") or "").strip()
    try:
        photo_index = int(context.get("photo_index", 0))
    except Exception:
        photo_index = 0

    rep_norm = _normalize_spanish(reply)

    # 4) Detectar qué modelo quiere ver
    target_item = None
    target_model_name = ""

    # A) PRIORIDAD 1: Si last_interest existe y coincide con el mensaje, usarlo
    #    Esto evita que "fotos de la G9" muestre otro modelo
    if last_interest:
        interest_norm = _normalize_spanish(last_interest)
        # Extraer tokens relevantes del interés guardado (incluir g9, e5, g7, etc.)
        interest_tokens = [p for p in interest_norm.split() if len(p) >= 2 and p not in ["foton", "camion", "camión"]]

        # Verificar si el mensaje menciona el modelo de interés
        if any(tok in msg for tok in interest_tokens):
            for item in items:
                modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
                if _normalize_spanish(modelo) == interest_norm or any(tok in _normalize_spanish(modelo) for tok in interest_tokens):
                    target_item = item
                    target_model_name = modelo
                    break

    # B) PRIORIDAD 2: Buscar mención explícita en mensaje o respuesta del bot (con scoring)
    if not target_item:
        best_item = None
        best_model = ""
        best_score = 0

        for item in items:
            modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
            if not modelo:
                continue

            modelo_norm = _normalize_spanish(modelo)
            # CAMBIO: Permitir tokens de 2 caracteres (g9, e5, g7, etc.)
            parts = [p for p in modelo_norm.split() if len(p) >= 2 and p not in ["foton", "camion", "camión"]]

            score = 0
            for part in parts:
                if part in msg:
                    score += 3  # Match en mensaje del usuario = alta prioridad
                if part in rep_norm:
                    score += 1  # Match en respuesta del bot = menor prioridad

            if score > best_score:
                best_score = score
                best_item = item
                best_model = modelo

        if best_score >= 2:  # Mínimo 2 puntos para considerar
            target_item = best_item
            target_model_name = best_model

    # C) PRIORIDAD 3: Usar last_interest sin mención (para "otra foto" sin decir modelo)
    if not target_item and last_interest:
        for item in items:
            modelo = _safe_get(item, ["Modelo", "modelo", "id_modelo"]).strip()
            if _normalize_spanish(modelo) == _normalize_spanish(last_interest):
                target_item = item
                target_model_name = modelo
                break

    if not target_item:
        return []

    # 5) Extraer fotos
    urls = _extract_photos_from_item(target_item)
    if not urls:
        return []

    # 6) Si cambió de modelo, reiniciar índice
    if _normalize_spanish(target_model_name) != _normalize_spanish(current_photo_model):
        photo_index = 0
        context["photo_model"] = target_model_name

    # 7) Determinar si quiere "otra" (1 foto) o "fotos" (grupo)
    wants_next = any(k in msg for k in ["otra", "mas", "más", "siguiente"])
    selected_urls: List[str] = []

    if wants_next:
        # Modo "Siguiente": manda 1 foto y avanza el índice
        if photo_index < len(urls):
            selected_urls = [urls[photo_index]]
            photo_index += 1
        else:
            # Ya no hay más, reiniciar (loop)
            photo_index = 0
            selected_urls = [urls[0]]
            photo_index = 1
    else:
        # Modo "Ver fotos": manda batch (ej. 3)
        batch_size = 3
        end_index = min(photo_index + batch_size, len(urls))
        selected_urls = urls[photo_index:end_index]
        if not selected_urls:
            photo_index = 0
            end_index = min(batch_size, len(urls))
            selected_urls = urls[0:end_index]
            photo_index = end_index
        else:
            photo_index = end_index

    # 8) Guardar el nuevo índice en contexto
    context["photo_index"] = photo_index
    return selected_urls


def _sanitize_reply_if_photos_attached(reply: str, media_urls: List[str]) -> str:
    if not media_urls:
        return reply

    bad_phrases = [
        r"no\s+puedo\s+enviar\s+fotos",
        r"no\s+puedo\s+mandar\s+fotos",
        r"no\s+tengo\s+fotos",
        r"no\s+puedo\s+enviar\s+im[aá]genes",
        r"no\s+puedo\s+mandar\s+im[aá]genes",
        r"soy\s+una\s+ia",
        r"soy\s+un\s+modelo",
    ]

    cleaned = reply or ""
    for p in bad_phrases:
        cleaned = re.sub(p, "Claro, aquí tienes.", cleaned, flags=re.IGNORECASE)

    return cleaned


def _strip_markdown_links(text: str) -> str:
    """
    Convierte links markdown [texto](url) a solo el URL.
    WhatsApp no soporta markdown links y se ven mal.
    Ejemplo: '[Ubicación](https://maps.app.goo.gl/xxx)' -> 'https://maps.app.goo.gl/xxx'
    """
    if not text:
        return text
    # Pattern: [cualquier texto](url)
    # Reemplaza con solo la URL
    return re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\2', text)


# ============================================================
# MONDAY VALIDATION (HARD GATE)
# ============================================================
def _lead_is_valid(lead: Dict[str, Any]) -> bool:
    if not isinstance(lead, dict):
        return False

    nombre = str(lead.get("nombre", "")).strip()
    interes = str(lead.get("interes", "")).strip()
    cita = str(lead.get("cita", "")).strip()

    if not nombre or len(nombre) < 3:
        return False

    placeholders = {"cliente nuevo", "desconocido", "amigo", "cliente", "nuevo lead", "usuario", "no proporcionado"}
    if nombre.lower() in placeholders:
        return False

    if not re.search(r"[a-zA-ZÁÉÍÓÚÑÜáéíóúñü]", nombre):
        return False

    if not interes or len(interes) < 2:
        return False

    if not cita or len(cita) < 2:
        return False

    return True


# ============================================================
# MAIN ENTRY
# ============================================================
async def handle_message(
    user_message: str,
    inventory_service,
    state: str,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    user_message = user_message or ""
    context = context or {}
    history = (context.get("history") or "").strip()

    # Silence mode
    if user_message.strip().lower() == "/silencio":
        new_history = (history + f"\nC: {user_message}\nA: Perfecto. Modo silencio activado.").strip()
        return {
            "reply": "Perfecto. Modo silencio activado.",
            "new_state": "silent",
            "context": {"history": new_history[-4000:]},
            "media_urls": [],
            "lead_info": None,
        }

    if state == "silent":
        return {
            "reply": "",
            "new_state": "silent",
            "context": context,
            "media_urls": [],
            "lead_info": None,
        }

    # Persistent context
    saved_name = (context.get("user_name") or "").strip()
    last_interest = (context.get("last_interest") or "").strip()
    last_appointment = (context.get("last_appointment") or "").strip()
    last_payment = (context.get("last_payment") or "").strip()
    try:
        turn_count = int(context.get("turn_count", 0)) + 1
    except (ValueError, TypeError):
        turn_count = 1

    # Extract from user input
    extracted_name = _extract_name_from_text(user_message)
    if extracted_name:
        saved_name = extracted_name

    extracted_payment = _extract_payment_from_text(user_message)
    if extracted_payment:
        last_payment = extracted_payment

    extracted_appt = _extract_appointment_from_text(user_message)
    if extracted_appt:
        last_appointment = extracted_appt

    # Time and date
    now_dt, current_time_str = get_mexico_time()
    # Formatear fecha en español manualmente (el servidor tiene locale inglés)
    meses_es = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    dias_es = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo"
    }
    current_date_str = f"{dias_es[now_dt.weekday()]} {now_dt.day} de {meses_es[now_dt.month]} de {now_dt.year}"

    formatted_system_prompt = SYSTEM_PROMPT.format(
        current_time_str=current_time_str,
        current_date_str=current_date_str,
        user_name_context=saved_name if saved_name else "(Aún no dice su nombre)",
        turn_number=turn_count,
    )

    inventory_text = _build_inventory_text(inventory_service)
    financing_text = _build_financing_text()

    context_block = (
        f"TURNO: {turn_count} {'(PRIMER MENSAJE - puedes saludar)' if turn_count == 1 else '(NO saludes, ve directo al punto)'}\n"
        f"MOMENTO ACTUAL: {current_time_str}\n"
        f"CLIENTE DETECTADO: {saved_name or '(Desconocido)'}\n"
        f"INTERÉS DETECTADO: {last_interest or '(Sin modelo)'}\n"
        f"CITA DETECTADA: {last_appointment or '(Sin cita)'}\n"
        f"PAGO DETECTADO: {last_payment or '(Por definir)'}\n"
        f"INVENTARIO DISPONIBLE:\n{inventory_text}\n\n"
        f"{financing_text}\n\n"
        f"HISTORIAL DE CHAT:\n{history[-3000:]}"
    )

    messages = [
        {"role": "system", "content": formatted_system_prompt},
        {"role": "user", "content": context_block},
        {"role": "user", "content": user_message},
    ]

    lead_info: Optional[Dict[str, Any]] = None
    reply_clean = "Hubo un error técnico."

    try:
        _MAX_RETRIES = 3
        for _attempt in range(_MAX_RETRIES):
            try:
                resp = await client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=350,
                )
                break
            except (APITimeoutError, RateLimitError) as e:
                if _attempt < _MAX_RETRIES - 1:
                    backoff = 2 ** (_attempt + 1)
                    logger.warning(f"⚠️ OpenAI retry {_attempt + 1}/{_MAX_RETRIES} tras {backoff}s: {e}")
                    await asyncio.sleep(backoff)
                else:
                    raise
            except APIStatusError as e:
                if e.status_code >= 500 and _attempt < _MAX_RETRIES - 1:
                    backoff = 2 ** (_attempt + 1)
                    logger.warning(f"⚠️ OpenAI 5xx retry {_attempt + 1}/{_MAX_RETRIES} tras {backoff}s: {e}")
                    await asyncio.sleep(backoff)
                else:
                    raise

        raw_reply = resp.choices[0].message.content or ""
        reply_clean = raw_reply

        # Update interest using user+bot text
        inferred_interest = _extract_interest_from_messages(user_message, raw_reply, inventory_service)
        if inferred_interest:
            last_interest = inferred_interest

        # Extract optional JSON from the model (inside ```json ... ```)
        json_match = re.search(r"```json\s*({.*?})\s*```", raw_reply, flags=re.DOTALL | re.IGNORECASE)
        if json_match:
            try:
                payload = json.loads(json_match.group(1))
                candidate = payload.get("lead_event") if isinstance(payload, dict) else None

                if isinstance(candidate, dict):
                    # Inject what we already know
                    if not str(candidate.get("nombre", "")).strip() and saved_name:
                        candidate["nombre"] = saved_name
                    if not str(candidate.get("interes", "")).strip() and last_interest:
                        candidate["interes"] = last_interest
                    if not str(candidate.get("cita", "")).strip() and last_appointment:
                        candidate["cita"] = last_appointment
                    if not str(candidate.get("pago", "")).strip() and last_payment:
                        candidate["pago"] = last_payment

                    if _lead_is_valid(candidate):
                        lead_info = candidate
                        logger.info(f"✅ Lead extraído del JSON de OpenAI: {candidate}")
                    else:
                        logger.warning(f"Lead JSON discarded (incomplete): {candidate}")

                # Hide JSON from user-facing message
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()
            except Exception as e:
                logger.error(f"Error parseando JSON de lead: {e}")
                reply_clean = raw_reply.replace(json_match.group(0), "").strip()

    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        reply_clean = "Dame un momento, estoy consultando sistema..."

    # Clean prefixes
    reply_clean = re.sub(
        r"^(Adrian|Asesor|Bot)\s*:\s*",
        "",
        reply_clean.strip(),
        flags=re.IGNORECASE,
    ).strip()

    # 🔥 CAMBIO CLAVE: Construir new_context ANTES de llamar a _pick_media_urls
    new_context: Dict[str, Any] = {
        "history": (history + f"\nC: {user_message}\nA: {reply_clean}").strip()[-4000:],
        "user_name": saved_name,
        "last_interest": last_interest,
        "last_appointment": last_appointment,
        "last_payment": last_payment,
        "turn_count": turn_count,
        # Mantener valores previos de fotos si existen
        "photo_model": context.get("photo_model"),
        "photo_index": context.get("photo_index", 0),
        # Mantener tipo de PDF solicitado para peticiones genéricas
        "last_pdf_request_type": context.get("last_pdf_request_type"),
    }

    # Pasamos new_context (la función lo modificará)
    media_urls = _pick_media_urls(user_message, reply_clean, inventory_service, new_context)
    reply_clean = _sanitize_reply_if_photos_attached(reply_clean, media_urls)

    # Quitar markdown links que WhatsApp no soporta
    reply_clean = _strip_markdown_links(reply_clean)

    # ============================================================
    # MONDAY FAILSAFE (MEJORADO - AGRESIVO)
    # ============================================================
    if lead_info is None:
        candidate = {
            "nombre": saved_name,
            "interes": last_interest,
            "cita": last_appointment,
            "pago": last_payment or "Por definir",
        }

        # CAMBIO 1: Validar ANTES de esperar confirmación
        if _lead_is_valid(candidate):
            lead_info = candidate
            logger.info(f"✅ FAILSAFE: Lead válido encontrado sin JSON de OpenAI - {candidate}")
        
        # CAMBIO 2: Si hay nombre + interés + cita, Y el mensaje es corto (posible confirmación)
        elif saved_name and last_interest and last_appointment:
            # Verificar si el mensaje es una confirmación o respuesta corta
            if _message_confirms_appointment(user_message) or len(user_message.strip()) <= 15:
                # Forzar registro aunque falte algo
                candidate["pago"] = candidate.get("pago") or "Por definir"
                if _lead_is_valid(candidate):
                    lead_info = candidate
                    logger.info(f"✅ FAILSAFE AGRESIVO: Mensaje corto '{user_message}' después de cita confirmada - {candidate}")

    # Log para debugging de leads
    if saved_name and last_interest and last_appointment:
        if lead_info:
            logger.info(f"🎯 LEAD SERÁ ENVIADO A MONDAY: {lead_info}")
        else:
            logger.warning(
                f"⚠️ LEAD NO GENERADO aunque hay datos: "
                f"nombre={saved_name}, interes={last_interest}, cita={last_appointment}, "
                f"mensaje_usuario='{user_message}'"
            )

    # ============================================================
    # FUNNEL STAGE CALCULATION
    # ============================================================
    # Determinar etapa del funnel basado en datos de la conversación
    # Mensaje → Enganche → Intención → Cita agendada
    funnel_stage = "Mensaje"  # Default: primer contacto

    if turn_count > 1:
        funnel_stage = "Enganche"  # Cliente respondió, hay interacción

    if last_interest:
        funnel_stage = "Intención"  # Modelo específico mencionado

    if last_appointment:
        funnel_stage = "Cita agendada"  # Cita confirmada

    # Agregar etapa al contexto para tracking
    new_context["funnel_stage"] = funnel_stage

    # ============================================================
    # PDF DETECTION (FICHA TÉCNICA / CORRIDA)
    # ============================================================
    pdf_info = _detect_pdf_request(user_message, last_interest, new_context)
    if pdf_info:
        # Guardar tipo de PDF solicitado para peticiones genéricas posteriores
        if pdf_info.get("tipo"):
            new_context["last_pdf_request_type"] = pdf_info.get("tipo")

        if pdf_info.get("sin_modelo"):
            # No hay modelo detectado, el bot debe preguntar
            logger.info(f"📄 PDF solicitado ({pdf_info.get('tipo')}) pero sin modelo detectado")
        elif pdf_info.get("sin_pdf"):
            # No tenemos el PDF de ese modelo
            logger.info(f"📄 PDF solicitado ({pdf_info.get('tipo')}) pero no disponible para {pdf_info.get('modelo')}")
        elif pdf_info.get("pdf_url"):
            # Tenemos el PDF, lo vamos a enviar
            logger.info(f"📄 PDF detectado: {pdf_info.get('tipo')} - {pdf_info.get('modelo')} - {pdf_info.get('filename')}")
            # Reemplazar la respuesta del bot con el mensaje apropiado
            reply_clean = pdf_info.get("mensaje", reply_clean)

    return {
        "reply": reply_clean,
        "new_state": "chatting",
        "context": new_context,
        "media_urls": media_urls,
        "lead_info": lead_info,
        "funnel_stage": funnel_stage,
        "funnel_data": {
            "nombre": saved_name or None,
            "interes": last_interest or None,
            "cita": last_appointment or None,
            "pago": last_payment or None,
            "turn_count": turn_count,
        },
        "pdf_info": pdf_info,
    }
