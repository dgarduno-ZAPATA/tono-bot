# Lógica de Negocio y Flujos - Tono-Bot (Tractos y Max)

Documento que describe toda la lógica de negocio, reglas de estado y flujos transaccionales del asistente virtual.

## Sección 1: Acciones Transaccionales (Interacción Activa)
Acciones en tiempo real ejecutadas durante la conversación con el cliente:

* **Crear Lead en Monday.com (1er Contacto)**
    * *Trigger:* Primer mensaje del cliente.
    * *Condiciones:* `remoteJid` válido, no es un grupo, no es un broadcast.
    * *Acción:* Crea ítem en Monday.com con stage "1er Contacto", asigna grupo mensual dinámico (ej. "FEBRERO 2026") y guarda variables de atribución (Referral).

* **Detección de Intención (Avance a "Intención")**
    * *Trigger:* Cliente menciona modelo específico de vehículo.
    * *Condiciones:* `last_interest` detectado (score ≥ 2 en matching de tokens vs. inventario activo).
    * *Acción:* Avanza stage a "Intención", actualiza columna "Vehículo de Interés" mediante un mapeo de sinónimos (`VEHICLE_DROPDOWN_MAP`).

* **Envío de Cotización/PDF (Avance a "Cotización")**
    * *Trigger:* Cliente solicita ficha técnica o corrida financiera.
    * *Condiciones:* Keywords detectados (acción + documento) + modelo identificado + PDF disponible en `financing.json`.
    * *Acción:* Envia un mensaje de texto preparatorio, seguido del archivo PDF. Avanza stage a "Cotización".

* **Cita Programada (Avance a "Cita Programada")**
    * *Trigger:* Cliente confirma cita con día y hora.
    * *Condiciones:* `nombre` (≥ 3 caracteres, sin palabras clave de rechazo) + `interes` (≥ 2 caracteres) + `cita` (≥ 2 caracteres).
    * *Acción:* Actualiza el lead en Monday.com, avanza a "Cita Programada", formatea la fecha en formato ISO, y notifica al owner (si `OWNER_PHONE` está configurado).

* **Detección de Método de Pago**
    * *Trigger:* Cliente menciona su forma de pago.
    * *Condiciones:* Keywords como "contado", "cash", "crédito", "financiamiento" (incluyendo patrones de negación).
    * *Acción:* Actualiza el contexto en memoria y la columna de "Esquema de Pago" en Monday ("De Contado", "Financiamiento" o "Por definir").

* **Detección de Desinterés (Override a "Sin Interés")**
    * *Trigger:* Frases explícitas de rechazo.
    * *Condiciones:* Match exacto o parcial en lista de frases ("no me interesa", "cancela", "STOP", "BAJA", etc.).
    * *Acción:* Fuerza el stage a "Sin Interes" (estado terminal). Un próximo contacto creará un ítem nuevo.

* **Procesamiento Multimedia (Audio y Visión)**
    * *Trigger:* Cliente envía nota de voz (`audioMessage`) o fotografía (`imageMessage`).
    * *Acción (Audio):* Descarga, desencripta y transcribe mediante Whisper API. Pasa el texto resultante a la lógica del bot.
    * *Acción (Imagen):* Analiza con Gemini Vision/OpenAI pidiendo una descripción breve enfocada en vehículos o documentos. Inyecta la descripción al contexto del LLM.

* **Envío de Fotos (Carrusel Dinámico)**
    * *Trigger:* Cliente solicita fotos ("mándame fotos", "siguiente", "otra").
    * *Condiciones:* Modelo identificado en inventario activo.
    * *Acción:* Envía batch de 3 fotos o 1 foto (siguiente), actualizando el índice (`photo_index`) en el contexto SQLite para mantener el seguimiento del carrusel.

* **Detección de Handoff Humano**
    * *Trigger:* Mensaje saliente (`fromMe=true`) no originado por el bot.
    * *Condiciones:* El ID del mensaje no está en `bot_sent_message_ids`, el texto no coincide con caché reciente, fuera de ventana temporal de seguridad y no es un mensaje automático de WhatsApp Business.
    * *Acción:* Silencia el bot para ese usuario durante `AUTO_REACTIVATE_MINUTES` (60 min por defecto).

* **Tracking de Referral (CTWA/Meta Ads)**
    * *Trigger:* Primer mensaje recibido que contiene `referral` object (Cloud API) o `contextInfo.conversionSource` (Baileys).
    * *Condiciones:* El `remoteJid` no tiene referral previo guardado en contexto.
    * *Acción:* Extrae origen, canal, `ad_id` y `ctwa_clid`, persiste en sesión y actualiza columnas dedicadas en Monday.com ("Origen Lead", "Canal", "Tipo Origen").

## Sección 2: Acciones de Gestión de Estados
Acciones asíncronas, estructurales y basadas en temporizadores:

* **Acumulación de Mensajes (Debouncing)**
    * *Timer:* 8 segundos (`MESSAGE_ACCUMULATION_SECONDS`).
    * *Condición:* Cliente envía múltiples mensajes seguidos en ráfaga.
    * *Acción:* Combina todos los textos, aplica candados de concurrencia (`processing_locks` por JID) para evitar condiciones de carrera, y procesa como una única transacción de IA.

* **Reactivación Automática del Bot**
    * *Timer:* 60 minutos (`AUTO_REACTIVATE_MINUTES`).
    * *Condición:* Bot silenciado previamente por handoff humano o comando `/silencio`.
    * *Acción:* Remueve el lock temporal de `silenced_users` permitiendo al bot responder nuevamente.

* **Refresh de Inventario Vehicular**
    * *Timer:* 300 segundos (`INVENTORY_REFRESH_SECONDS`).
    * *Condición:* Caché de memoria ha expirado.
    * *Acción:* Descarga CSV desde URL (Google Sheets) o archivo local. Aplica filtros de unidades agotadas (Cantidad ≤ 0) o con status no disponible.

## Sección 3: Reglas de Validación y Seguridad

* **Name Gate (Candado de Nombre):** El prompt inyecta advertencias estrictas de que el bot *no puede* dar precios, cotizaciones ni agendar citas si no ha extraído previamente un nombre válido del cliente.
* **Anti-Alucinación Estricta:** El bot tiene prohibido inventar vehículos. Si un modelo no aparece en el volcado semántico del inventario inyectado en el prompt, el bot debe indicar que no se maneja y sugerir opciones reales.
* **Gestión de Carga vs Pasajeros:** Regla semántica de interpretación. Si el vehículo en inventario es panel o chasis y el usuario pregunta "cuántos caben", el bot debe aclarar obligatoriamente que son de carga y los asientos son solo en cabina.
* **Deduplicación de Memoria:** Se utilizan `BoundedOrderedSet` para rastrear IDs de mensajes procesados (límite 4000) y IDs de leads procesados (límite 8000) previniendo fugas de memoria y bucles de webhook.

## Sección 4: Progresión del Embudo (Funnel V2)

El bot sigue estrictamente la jerarquía V2 de Monday.com.
* **Regla de Oro (Solo Avance):** El estado del lead nunca puede retroceder de rango. `STAGE_HIERARCHY` valida matemáticamente el avance.
* **Estados Terminales (Nuevo Ciclo de Ventas):** Si el lead actual se encuentra en `Venta Cerrada`, `Venta Caida` o `Sin Interes`, el bot NO actualiza el registro. Crea un registro completamente nuevo para iniciar un nuevo ciclo.

**Jerarquía de Avance:**
1. `1er Contacto` (Automático al primer mensaje)
2. `Intención` (Modelo detectado)
3. `Cotización` (Ficha / Corrida enviada)
4. `Cita Programada` (Día/Hora acordados)
* *Override Global:* `Sin Interes` (Detiene el flujo en cualquier punto).

---
*Archivos fuente referenciados:*
* `/src/conversation_logic.py` - Prompt principal, reglas IA, extracción de variables (Nombre, Cita, Pago), Lógica de PDFs y carrusel visual.
* `/src/main.py` - Webhooks, debouncing, procesamiento de Whisper/Vision, referrals, delays humanos, handoff.
* `/src/monday_service.py` - Mutaciones GraphQL, mapeo de dropdowns (`VEHICLE_DROPDOWN_MAP`), formato de fechas ISO, jerarquía de embudo.
* `/src/inventory_service.py` - Parser de catálogo, exclusión de agotados y TTL de caché.
