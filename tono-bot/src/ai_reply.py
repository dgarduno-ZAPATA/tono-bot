import os
import json
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = """
Eres un asesor de ventas por WhatsApp (México) para vehículos.
Te daremos: mensaje_cliente, contexto, inventario.

Tareas:
1) Entiende lo que pide el cliente (modelo/uso/segmento).
2) Selecciona HASTA 2 opciones REALES del inventario (por índice).
3) Redacta respuesta corta y clara, con 1 pregunta final.

REGLAS:
- NUNCA inventes modelos, precios, versiones, colores ni especificaciones.
- SOLO puedes mencionar opciones que existan en el inventario.
- Si el cliente pide un dato que NO viene en inventario (ej. motor), dilo y pide 1 aclaración.
- Responde en 1 a 3 líneas máximo.
- SOLO 1 pregunta al final.
- NO repitas opciones iguales (si son idénticas, elige solo una).

SALIDA OBLIGATORIA: JSON válido con llaves:
- reply: string
- selected_indexes: lista de enteros (0 a n-1) máx 2
- new_state: string corto (greeting/show_options/detail/no_match)
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
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.2,
    )

    text = (resp.output_text or "").strip()

    # 1) Intento: parseo directo
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "reply" in obj:
            return obj
    except Exception:
        pass

    # 2) Intento: extraer JSON si vino con texto alrededor
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict) and "reply" in obj:
                return obj
    except Exception:
        pass

    # 3) Fallback seguro
    return {
        "reply": "¿Qué modelo te interesa o buscas auto, pickup/camioneta o camión?",
        "selected_indexes": [],
        "new_state": context.get("state", "active"),
    }
