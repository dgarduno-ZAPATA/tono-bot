import os
import json
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = """
Eres un asesor de ventas por WhatsApp (México) para vehículos (autos, pickups/camionetas, camiones).
Reglas OBLIGATORIAS:
- Responde en 1 a 3 líneas máximo.
- Usa máximo 2 opciones (no 3) a menos que el cliente pida “más opciones”.
- Haz SOLO 1 pregunta al final.
- No repitas saludo si ya saludaste antes.
- Si el cliente pide cita con fecha/hora, CONFIRMA y pide solo el nombre.
- Nunca escribas párrafos largos.
"""

def generate_reply(user_text: str, inventory_rows: list[dict]) -> str:
    payload = {
        "mensaje_cliente": user_text,
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
