import os
import json
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = """
Eres un asesor de ventas por WhatsApp (México) para vehículos.
Te daremos: mensaje_cliente, contexto, inventario.

Tareas:
1) Identifica si el cliente pide un modelo específico (ej. 'Tunland G9').
2) Selecciona HASTA 2 opciones REALES del inventario (por índice).
3) Redacta respuesta corta y clara, con 1 pregunta final.

REGLAS:
- NUNCA inventes modelos, precios o versiones.
- SOLO puedes mencionar opciones que existan en el inventario.
- Si el modelo solicitado NO existe, dilo y ofrece 1-2 alternativas del inventario.
- Responde en 1 a 3 líneas.
- NO repitas opciones iguales (si son idénticas, elige solo una).

SALIDA OBLIGATORIA: JSON válido con llaves:
- reply: string
- selected_indexes: lista de enteros (0 a n-1)
- new_state: string corto (ej. greeting/show_options/detail/booking)
"""

def generate_reply(user_text: str, inventory_rows: list[dict], context: dict) -> dict:
    payload = {
        "mensaje_cliente": user_text,
        "contexto": context,
        "inventario": inventory_rows[:80],
    }

    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        ],
        temperature=0.2,
    )

    text = (resp.output_text or "").strip()

    # Intento 1: parseo directo
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "reply" in obj:
            return obj
    except Exception:
        pass

    # Intento 2: por si el modelo envió texto alrededor, extraemos el JSON
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = json.loads(text[start:end+1])
            if isinstance(obj, dict) and "reply" in obj:
                return obj
    except Exception:
        pass

    # Fallback seguro (nunca enviamos JSON al usuario)
    return {
        "reply": "¿Qué modelo te interesa o buscas auto, pickup/camioneta o camión?",
        "selected_indexes": [],
        "new_state": context.get("state", "active")
    }
