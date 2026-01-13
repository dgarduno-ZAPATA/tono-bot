from src.ai_reply import generate_reply

def handle_message(message, inventory_service):
    inv = inventory_service.items if hasattr(inventory_service, "items") else []
    return generate_reply(message, inv)
