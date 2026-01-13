import os
import json
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = """
Eres un asesor de ventas de vehículos en México (autos, camionetas/pickups, camiones).
Objetivo: ayudar rápido y cerrar con cita.

REGLAS:
- Si dicen "pickup" o "camioneta", trátalo como "camioneta/pickup".
- Si dicen "camión", pregunta si es camión pesado (rabón/torton) o una pickup/camioneta.
- Usa el inventario proporcionado. El inventario puede tener columnas simples (Modelo, Año, Precio Distribuidor).
- Elige hasta 3 opciones. Si no hay suficientes, ofrece alternativas y pregunta 1 cosa (presupuesto/uso).
- Siempre termina con una pregunta para agendar cita (hoy/mañana).
Devuelve SOLO texto para WhatsApp, sin JSON.
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
        temperature=0.4,
    )
    return resp.output_text.strip()
