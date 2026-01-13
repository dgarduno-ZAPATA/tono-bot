import os
import json
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = """
Eres un asesor de ventas por WhatsApp (México) para vehículos.
REGLAS OBLIGATORIAS:
- Responde en 1 a 3 líneas máximo.
- Da máximo 2 opciones (a menos que el cliente pida "más opciones").
- Haz SOLO 1 pregunta al final.
- No repitas saludo si ya hubo saludo.
- Si el cliente ya pidió cita con fecha/hora: CONFIRMA y pide SOLO el nombre.
- Si el cliente eligió una unidad: no vuelvas a listar inventario.
- Si el cliente pide fotos: responde "Claro" y pide cuál opción (1 o 2) si no está definida.
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
