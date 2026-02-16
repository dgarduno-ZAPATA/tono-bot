# PROPUESTA DE BLINDAJE PROFESIONAL — TONO-BOT

**Version:** 1.0
**Fecha:** 16 de febrero de 2026
**Preparado por:** Equipo de Desarrollo Tono-Bot
**Dirigido a:** Direccion General — Tractos y Max

---

## RESUMEN EJECUTIVO

Tono-Bot es el asistente virtual de ventas por WhatsApp de Tractos y Max. Actualmente esta en produccion atendiendo clientes reales, pero la auditoria tecnica del codigo revelo **2 vulnerabilidades criticas** y **3 riesgos operativos altos** que ponen en juego:

- **Leads perdidos** por reinicio del servidor (la base de datos se borra)
- **Mensajes falsos** a clientes (el webhook no valida quien le envia datos)
- **Gasto no controlado** de OpenAI (sin limite de peticiones por minuto)
- **Riesgo legal** por almacenamiento de datos personales sin cifrado

Esta propuesta presenta **7 tareas concretas** para blindar el bot en **3 semanas**, con un costo mensual total de **$32 USD (~$576 MXN/mes)**.

### Contexto de negocio

Un camion Foton se vende entre **$300,000 y $720,000 MXN**. Si una falla del bot causa la perdida de **un solo lead que hubiera cerrado**, el costo supera **anios enteros** de la inversion propuesta. Esta no es una mejora opcional — es proteccion de ingresos.

---

## SITUACION ACTUAL (HALLAZGOS DE AUDITORIA)

| # | Hallazgo | Severidad | Evidencia en codigo | Riesgo de negocio |
|---|----------|-----------|--------------------|--------------------|
| 1 | **Webhook sin autenticacion** | CRITICA | `main.py:1032` — El endpoint `/webhook` acepta POST de cualquier origen. Sin firma, token ni verificacion. | Cualquier persona que descubra la URL puede enviar mensajes falsos a clientes o gastar tokens de OpenAI. |
| 2 | **Sin rate limiting** | CRITICA | No existe middleware de limite de peticiones en todo el proyecto. | Ataque DDoS podria tumbar el bot, gastar presupuesto de OpenAI, o causar ban del numero de WhatsApp por Meta. |
| 3 | **Base de datos efimera (SQLite local)** | ALTA | `memory_store.py:7` — DB en `/app/tono-bot/db/memory.db` dentro del contenedor. | Cada reinicio de Render **borra todas las conversaciones**. Se pierden nombres, citas, intereses de compra. |
| 4 | **Datos personales sin cifrado** | ALTA | `memory_store.py:17-24` — Telefonos, nombres y preferencias de pago en texto plano. | Incumplimiento potencial de LFPDPPP. En caso de brecha, exposicion directa de datos de clientes. |
| 5 | **Docker corre como root** | MEDIA | `Dockerfile:1-23` — Sin instruccion `USER`. Sin `.dockerignore`. `COPY . .` copia todo, incluyendo potenciales `.env` y `.git`. | Si el contenedor es comprometido, el atacante tiene privilegios de administrador. |
| 6 | **Bot puede inventar precios** | MEDIA | `conversation_logic.py:89` — Hay regla "NUNCA inventes precios" pero sin validacion programatica. El modelo puede alucinar. | Riesgo legal y comercial: cliente llega al showroom con un precio que el bot invento. |
| 7 | **Sin monitoreo de caidas** | MEDIA | No existe monitoreo externo del endpoint `/health`. | El bot puede estar caido horas sin que nadie lo note. Leads perdidos silenciosamente. |

---

## LAS 7 TAREAS PROPUESTAS

---

### TAREA 1: Validacion de Webhook (Firma/Token)

**Problema que resuelve:**
Actualmente, el endpoint `/webhook` (`main.py:1032`) acepta cualquier peticion POST sin verificar su origen. Esto significa que si un atacante o un bot scanner descubre la URL de Render, puede:
- Enviar mensajes falsos a clientes reales
- Gastar tokens de OpenAI con peticiones fraudulentas
- Inyectar informacion incorrecta en el CRM (Monday.com)

**Solucion tecnica:**
Agregar un header secreto (`X-Webhook-Secret`) que solo Evolution API y nuestro servidor conocen. Toda peticion sin este header se rechaza con error 401.

**Cambios en codigo:**

```
Archivo: main.py
- Nueva variable de entorno: WEBHOOK_SECRET
- Nuevo middleware de verificacion antes de procesar webhooks
- Respuesta 401 Unauthorized para peticiones sin token valido
```

**Configuracion requerida:**
- Crear variable `WEBHOOK_SECRET` en Render (string aleatorio de 64 caracteres)
- Configurar el mismo secreto en Evolution API como header de webhook

| Metrica | Valor |
|---------|-------|
| Esfuerzo estimado | 3 horas |
| Costo recurrente | $0 |
| Archivos afectados | `main.py` |
| Riesgo de implementacion | Bajo |

---

### TAREA 2: Rate Limiting en `/webhook`

**Problema que resuelve:**
No existe limite de peticiones por minuto en ningun endpoint. Un ataque de volumen puede:
- Tumbar el servicio
- Agotar el presupuesto mensual de OpenAI en horas
- Causar **ban del numero de WhatsApp por Meta** (si el bot responde demasiado rapido a demasiados contactos)

**Solucion tecnica:**
Instalar `slowapi` (libreria open source MIT, gratuita, disenada para FastAPI) con limites por IP y por telefono del cliente.

**Limites propuestos:**

| Limite | Valor | Justificacion |
|--------|-------|---------------|
| Por IP | 60 peticiones/minuto | Previene DDoS y escaneo |
| Por telefono (JID) | 10 mensajes/minuto | Previene spam y protege contra ban de WhatsApp |
| Global | 300 peticiones/minuto | Techo general del servicio |

**Cambios en codigo:**

```
Archivo: main.py — Middleware de rate limiting con respuesta 429 Too Many Requests
Archivo: requirements.txt — Agregar: slowapi==0.1.9
```

| Metrica | Valor |
|---------|-------|
| Esfuerzo estimado | 2 horas |
| Costo recurrente | $0 (open source) |
| Archivos afectados | `main.py`, `requirements.txt` |
| Riesgo de implementacion | Bajo |

---

### TAREA 3: Docker Hardening

**3 sub-problemas que resuelve:**

**3A — Sin `.dockerignore`:**
La instruccion `COPY . .` del Dockerfile copia TODO al contenedor, incluyendo potencialmente archivos `.env` (con API keys), directorio `.git` (con historial completo), y archivos temporales.

**3B — Contenedor corre como root:**
El Dockerfile no especifica un usuario. Por defecto, la aplicacion corre con privilegios de administrador.

**3C — Sin health check nativo:**
Docker/Render no tienen forma automatica de saber si la aplicacion esta viva.

**Cambios en codigo:**

```
Archivo nuevo: .dockerignore
- Excluir: .env*, .git, *.pem, __pycache__, *.pyc, db/

Archivo: Dockerfile (modificado)
- Agregar usuario non-root (appuser)
- Agregar HEALTHCHECK cada 30 segundos
```

| Metrica | Valor |
|---------|-------|
| Esfuerzo estimado | 30 minutos |
| Costo recurrente | $0 |
| Archivos afectados | `Dockerfile`, nuevo `.dockerignore` |
| Riesgo de implementacion | Muy bajo |

---

### TAREA 4: Migracion de SQLite a Supabase (PostgreSQL)

**Problema que resuelve:**
Este es el riesgo operativo mas grave. La base de datos actual es un archivo SQLite **dentro del contenedor Docker**. Render es un servicio de hosting que **reinicia contenedores regularmente** (por mantenimiento, deploys, o escalado). Cada reinicio **borra la base de datos completa**.

Esto significa que Tono-Bot periodicamente "olvida":
- Nombres de clientes
- Vehiculos de interes
- Citas programadas
- Historial de conversacion
- Etapa del embudo de ventas

**Datos personales almacenados actualmente (sin cifrar):**

| Campo | Ejemplo | Riesgo LFPDPPP |
|-------|---------|----------------|
| Telefono (PRIMARY KEY) | `5215512345678` | Dato personal identificable |
| Nombre del cliente | `Juan Perez` | Dato personal identificable |
| Vehiculo de interes | `Tunland G9` | Dato comercial |
| Tipo de pago | `Financiamiento` | Dato financiero |
| Fecha de cita | `2026-02-20` | Dato de agenda |

**Plan seleccionado: Supabase Pro ($25 USD/mes)**

| Caracteristica | Free (no recomendado) | Pro (recomendado) |
|---------------|----------------------|-------------------|
| Precio | $0 | $25 USD/mes |
| Almacenamiento DB | 500 MB | 8 GB |
| Backups | No | Diarios, 7 dias retencion |
| Pausa automatica | Si (si no hay uso en 1 semana) | No |
| Soporte | Comunidad | Email |
| Para produccion | No | Si |

**Por que no el plan Free:** El plan gratuito **pausa automaticamente** proyectos sin actividad en 1 semana. Si el bot tiene un periodo lento (vacaciones, fin de semana largo), la base de datos se desconecta y el bot pierde acceso a toda la memoria. Inaceptable para produccion.

**Plan de migracion:**
1. Crear proyecto en Supabase
2. Crear tabla `sessions` con esquema identico
3. Habilitar Row-Level Security y cifrado
4. Reemplazar `memory_store.py` (misma interfaz get/upsert/close)
5. Probar con datos de prueba
6. Deploy a produccion
7. Verificar persistencia tras reinicio de Render

| Metrica | Valor |
|---------|-------|
| Esfuerzo estimado | 8 horas |
| Costo recurrente | **$25 USD/mes (~$450 MXN/mes)** |
| Archivos afectados | `memory_store.py`, `requirements.txt`, `main.py` |
| Riesgo de implementacion | Medio — requiere pruebas. Se puede hacer gradual. |

---

### TAREA 5: Guardrail Programatico de Precios

**Problema que resuelve:**
La regla "NUNCA inventes precios" existe solo como texto en el system prompt (`conversation_logic.py:89`). GPT puede ignorarla — y ya lo hizo una vez, dando $720,000 por una Tunland G9 que cuesta $450,000.

El prompt le dice al modelo que hacer, pero **no hay verificacion programatica** de que lo haya hecho. Si el modelo alucina un precio, ese precio llega directo al cliente por WhatsApp.

**Impacto real:**
- Cliente llega al showroom esperando un precio que no existe
- Posible queja ante PROFECO
- Perdida de credibilidad del negocio

**Solucion tecnica:**
Capa de validacion **despues** de que GPT genera la respuesta y **antes** de enviarla al cliente:

1. **Extraccion:** Regex detecta cualquier mencion de `$XXX,XXX` en la respuesta
2. **Validacion:** Comparar cada precio con los precios reales del inventario
3. **Correccion:** Si el precio no coincide (tolerancia +/-5%), reemplazar con el precio correcto del inventario

**Ejemplo de flujo:**

```
GPT genera:  "La Tunland G9 esta en $720,000 MXN"
Validacion:  $720,000 != $450,000 (precio real) -> CORREGIDO
Bot envia:   "La Tunland G9 esta en $450,000 MXN IVA incluido"
Log:         "PRECIO CORREGIDO: GPT=$720,000, Inventario=$450,000, Modelo=Tunland G9"
```

| Metrica | Valor |
|---------|-------|
| Esfuerzo estimado | 3 horas |
| Costo recurrente | $0 |
| Archivos afectados | `conversation_logic.py` |
| Riesgo de implementacion | Bajo — capa adicional, no modifica logica existente |

---

### TAREA 6: Monitoreo con Sentry + UptimeRobot

**Problema que resuelve:**
Si el bot se cae o tiene errores, **nadie se entera** hasta que un cliente se queja o alguien revisa los logs manualmente.

**6A — Sentry (errores en tiempo real):**
Captura automaticamente cada error y crash. Alerta inmediata por email.

| Caracteristica | Plan Developer (Free) |
|---------------|-----------------------|
| Precio | $0 |
| Errores/mes | 5,000 |
| Retencion | 7 dias |
| Suficiente para Tono-Bot | Si (volumen actual ~100-500 conversaciones/mes) |

**6B — UptimeRobot (monitoreo de disponibilidad):**
Ping al endpoint `/health` cada 60 segundos. Alerta inmediata si no responde.

| Caracteristica | Plan Solo |
|---------------|-----------|
| Precio | $7 USD/mes |
| Monitores | 10 |
| Intervalo | 60 segundos |
| Uso comercial | Si |

**Cambios en codigo:**

```
Archivo: main.py — Integracion de Sentry SDK (2 lineas en startup)
Archivo: requirements.txt — Agregar: sentry-sdk[fastapi]
Configuracion externa: Crear monitor en UptimeRobot apuntando a /health
```

| Metrica | Valor |
|---------|-------|
| Esfuerzo estimado | 1 hora |
| Costo recurrente | **$7 USD/mes (~$126 MXN/mes)** |
| Archivos afectados | `main.py`, `requirements.txt` |
| Riesgo de implementacion | Muy bajo — observacion pasiva |

---

### TAREA 7: Alerta de Abandono de Conversacion -> Monday.com

**Problema que resuelve:**
Si un cliente pregunta el precio de un camion y deja de responder, **nadie hace nada**. Ese lead se enfria y se pierde.

Esto es critico porque:
- Un lead que pregunto precio tiene **intencion de compra demostrada**
- La ventana de oportunidad es corta (horas, no dias)
- Un vendedor humano que llame a tiempo puede cerrar la venta

**Solucion tecnica:**
Sistema de deteccion de abandono que:
1. Detecta inactividad (cliente con interes detectado, sin respuesta en 2 horas)
2. Crea alerta en Monday.com con nota de seguimiento
3. Opcionalmente notifica al equipo por WhatsApp

**Reglas de activacion:**

| Condicion | Accion |
|-----------|--------|
| Cliente pregunto precio + 2h sin respuesta | Alerta "Lead tibio" en Monday.com |
| Cliente confirmo cita + no se presento (24h) | Alerta "Cita no atendida" en Monday.com |
| Cliente dijo modelo + 4h sin respuesta | Alerta "Seguimiento necesario" en Monday.com |

| Metrica | Valor |
|---------|-------|
| Esfuerzo estimado | 4 horas |
| Costo recurrente | $0 (usa Monday.com existente) |
| Archivos afectados | `main.py`, `monday_service.py` |
| Riesgo de implementacion | Bajo — funcionalidad aditiva |

---

## CRONOGRAMA DE IMPLEMENTACION

```
SEMANA 1 (Dias 1-5): Seguridad base
  Dia 1-2:  Tarea 1 — Validacion de webhook
  Dia 2-3:  Tarea 2 — Rate limiting
  Dia 3:    Tarea 3 — Docker hardening
  Dia 4-5:  Tarea 6 — Sentry + UptimeRobot

SEMANA 2 (Dias 6-12): Estabilidad y datos
  Dia 6-9:   Tarea 4 — Migracion a Supabase
  Dia 10-12: Pruebas de migracion y validacion

SEMANA 3 (Dias 13-18): Inteligencia comercial
  Dia 13-14: Tarea 5 — Guardrail de precios
  Dia 15-17: Tarea 7 — Alertas de abandono
  Dia 18:    Pruebas integrales y go-live
```

---

## RESUMEN DE COSTOS

### Inversion unica (desarrollo)

| Tarea | Horas estimadas |
|-------|----------------|
| 1. Validacion de webhook | 3 hrs |
| 2. Rate limiting | 2 hrs |
| 3. Docker hardening | 0.5 hrs |
| 4. Migracion Supabase | 8 hrs |
| 5. Guardrail de precios | 3 hrs |
| 6. Sentry + UptimeRobot | 1 hr |
| 7. Alertas de abandono | 4 hrs |
| **TOTAL** | **21.5 horas** |

### Costo mensual recurrente

| Servicio | Plan | Costo USD/mes | Costo MXN/mes (aprox.) |
|----------|------|---------------|------------------------|
| Supabase | Pro | $25.00 | $450 |
| UptimeRobot | Solo | $7.00 | $126 |
| Sentry | Developer (Free) | $0.00 | $0 |
| slowapi | Open source | $0.00 | $0 |
| **TOTAL MENSUAL** | | **$32.00 USD** | **~$576 MXN** |

### Costo anual

| Concepto | USD | MXN (aprox.) |
|----------|-----|--------------|
| Recurrente (12 meses) | $384 | $6,912 |
| **TOTAL ANUAL** | **$384 USD** | **~$6,912 MXN** |

---

## ANALISIS DE RETORNO (ROI)

### Escenario: Prevencion de perdida de 1 lead por trimestre

| Metrica | Valor |
|---------|-------|
| Precio promedio de un camion Foton | $450,000 MXN |
| Margen estimado por unidad (conservador 8%) | $36,000 MXN |
| Leads recuperados por mejoras (estimado conservador) | 1 por trimestre |
| **Beneficio anual estimado** | **$144,000 MXN** |
| **Costo anual de la propuesta** | **$6,912 MXN** |
| **ROI** | **1,983%** |

### Escenario: Prevencion de un incidente de seguridad

| Tipo de incidente | Costo estimado |
|-------------------|----------------|
| Envio de mensajes falsos a clientes | Crisis reputacional + potencial perdida de numero de WhatsApp |
| Ban del numero por Meta | Perdida de canal de comunicacion con TODOS los leads activos |
| Brecha de datos personales (LFPDPPP) | Multa de hasta $27,000,000 MXN (Art. 64 LFPDPPP) |
| Bot caido sin deteccion (1 semana) | Estimado 5-15 leads perdidos |

---

## QUE PASA SI NO SE HACE NADA

| Riesgo | Probabilidad | Impacto |
|--------|-------------|---------|
| Perdida de base de datos por reinicio de Render | **Alta** (Render reinicia semanalmente) | Tono-Bot "olvida" a todos los clientes |
| Inyeccion de mensajes falsos via webhook | **Media** (scanners prueban endpoints constantemente) | Mensajes no autorizados a clientes |
| Agotamiento de presupuesto OpenAI | **Media** (sin rate limiting) | Bot deja de funcionar |
| Bot cita precio incorrecto | **Alta** (ya ocurrio: Tunland G9 a $720k vs $450k) | Perdida de credibilidad |
| Ban del numero de WhatsApp por Meta | **Baja-Media** (sin rate limiting) | Perdida total del canal |

---

## CONDICIONES DE EXITO (Definition of Done)

| Tarea | Condicion de exito verificable |
|-------|-------------------------------|
| 1. Webhook validation | Peticion sin token -> respuesta 401. Peticion con token -> procesamiento normal. |
| 2. Rate limiting | 61+ peticiones/min desde misma IP -> respuesta 429. Trafico normal no afectado. |
| 3. Docker hardening | Contenedor corre como `appuser` (no `root`). `.env` ausente del contenedor. HEALTHCHECK reporta healthy. |
| 4. Supabase | Reiniciar contenedor en Render -> conversaciones persisten. Datos cifrados en reposo. |
| 5. Guardrail precios | Bot menciona precio -> precio coincide con inventario +/-5%. Precio inventado -> corregido automaticamente. |
| 6. Monitoreo | Bot caido 2 minutos -> alerta recibida por email. Error en codigo -> visible en Sentry. |
| 7. Alertas abandono | Cliente inactivo 2h con interes -> nota en Monday.com. Equipo notificado. |

---

## HERRAMIENTAS UTILIZADAS

| Herramienta | Tipo | Licencia | Sitio web |
|-------------|------|----------|-----------|
| Supabase | Base de datos PostgreSQL (nube) | Comercial (Pro) | supabase.com |
| Sentry | Monitoreo de errores | Freemium (Developer) | sentry.io |
| UptimeRobot | Monitoreo de disponibilidad | Comercial (Solo) | uptimerobot.com |
| slowapi | Rate limiting para FastAPI | Open source (MIT) | github.com/laurentS/slowapi |

---

## APROBACION

| | Nombre | Firma | Fecha |
|---|--------|-------|-------|
| Elaboro | | | |
| Reviso | | | |
| Aprobo (Direccion) | | | |

---

*Este documento es confidencial y de uso interno de Tractos y Max.*
