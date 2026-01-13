def handle_message(message, inventory_service):
    text = (message or "").strip().lower()

    # Saludo
    if "hola" in text or "buen" in text:
        return (
            "¡Hola! Soy Toño Ramírez 😊\n"
            "Te ayudo a encontrar el vehículo ideal.\n"
            "¿Buscas un *auto* o un *camión*?"
        )

    # Tipo de vehículo
    if "auto" in text:
        results = inventory_service.search(vehicle_category="auto")
        if results:
            v = results[0]
            return (
                f"Tengo esta opción:\n"
                f"{v.get('Marca','')} {v.get('Modelo','')} {v.get('Año','')} – ${v.get('Precio','')}\n\n"
                "¿Te gustaría agendar una cita para verlo?"
            )
        return "Por ahora no tengo autos disponibles."

    if "camion" in text or "camión" in text:
        results = inventory_service.search(vehicle_category="camion")
        if results:
            v = results[0]
            return (
                f"Tengo esta opción:\n"
                f"{v.get('Marca','')} {v.get('Modelo','')} {v.get('Año','')} – ${v.get('Precio','')}\n\n"
                "¿Te gustaría agendar una cita para verlo?"
            )
        return "Por ahora no tengo camiones disponibles."

    # Cita
    if text in ["si", "sí", "claro", "va", "ok", "dale"]:
        return "Perfecto ✅ ¿Te queda mejor venir *hoy* o *mañana*?"

    if "hoy" in text or "mañana" in text:
        return (
            "Excelente 👍\n"
            "Tu cita queda registrada.\n"
            "En breve te contactan para confirmar."
        )

    # Default
    return "Dime si buscas *auto* o *camión* y te muestro opciones."
