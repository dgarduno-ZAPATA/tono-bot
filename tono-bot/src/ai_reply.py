import os
import json
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = """
Eres un asesor de ventas por WhatsApp (México) para vehículos.
Te daremos:
- mensaje_cliente
- contexto (state y quizá focused_model)
- options (máximo 2) si hay

REGLAS OBLIGATORIAS:
- NUNCA inventes modelos, precios o versiones.
- Si options está vacío: NO inventes. Haz 1 sola pregunta para aclarar (modelo/uso/presupuesto).
- Responde en 1 a 3 líneas máximo.
- SOLO 1 pregunta al final.
- Si state = greeting: saluda y pregunta "¿Qué buscas: auto, pickup/camioneta o camión?" (sin listar inventario).
- Si state = show_options: muestra SOLO las 2 options (modelo/año/precio) y pregunta cuál le interesa (1 o 2).
- Si state = detail: da 2-3 características generales (sin inventar ficha técnica) + precio + pregunta si quiere verla hoy o mañana.
- Si state = clarify: pide confirmación del nombre del modelo (ej. “¿Te refieres a TOANO o TUNLAND?”).
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
