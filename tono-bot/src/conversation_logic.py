from src.ai_reply import generate_reply

def handle_message(message, inventory_service, state, context):
    inv = inventory_service.items if hasattr(inventory_service, "items") else []
    # Le pasamos a la IA el estado y memoria para que responda corto y con continuidad
    ctx = {"state": state, **(context or {})}
    return generate_reply(message, inv, ctx)
