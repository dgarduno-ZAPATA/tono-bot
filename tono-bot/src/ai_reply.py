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

def generate_reply(user_text: str, inventory_rows: list[dict], context: dict) -> str:
    payload = {
        "mensaje_cliente": user_text,
        "contexto": context,
        "inventario": inventory_rows[:50],
    }

    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        ],
        temperature=0.2,
    )
    return resp.output_text.strip()
