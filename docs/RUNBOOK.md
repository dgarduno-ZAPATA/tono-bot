# Runbook - Tono-Bot Operations

## Despliegue

### Plataforma
- **Google Cloud Run** (CI/CD via Cloud Build con trigger automatico en branch main)
- **Puerto**: 8080
- **Dockerfile**: Raiz del repo (`/Dockerfile`)
- **Base de datos**: Cloud SQL PostgreSQL 15 (instancia tono-bot-db, database tonobot, user tonobot_app)

### Variables de Entorno
Todas las variables se configuran en el servicio de Cloud Run. Ver `.env.example` para la lista completa.

### Health Check
```bash
curl https://tu-servicio-cloud-run/health
```

Respuesta incluye metricas del bot: uptime, sesiones activas, estado del LLM.

## Comandos de Desarrollo Local

### Instalar dependencias
```bash
cd tono-bot
pip install -r requirements.txt
```

### Ejecutar localmente
```bash
cd tono-bot
uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
```

### Docker
```bash
docker build -t tono-bot .
docker run -p 8080:8080 --env-file .env tono-bot
```

## Monitoreo

### Logs Clave

| Patron de Log | Significado |
|---------------|-------------|
| `Startup` | Aplicacion iniciando |
| `Smoke test` | Verificando conectividad Gemini |
| `Gemini unreachable` | Fallback a OpenAI activado |
| `Webhook received` | Mensaje entrante |
| `LLM response` | Respuesta generada |
| `Human detected` | Handoff humano detectado |
| `Silenced` | Bot silenciado para un JID |
| `Monday.com` | Operacion CRM |
| `Error` | Error que requiere atencion |

### Metricas en /health
- Estado de la conexion HTTP
- Proveedor LLM activo (Gemini/OpenAI)
- Conteo de sesiones activas
- Conteo de JIDs silenciados

## Troubleshooting

### Bot no responde

1. Verificar `/health` endpoint
2. Revisar logs para errores de conexion
3. Verificar que `EVOLUTION_API_URL` y `EVOLUTION_API_KEY` sean correctos
4. Verificar que la instancia de WhatsApp este conectada en Evolution API

### Gemini no funciona (fallback a OpenAI)

**Sintoma**: Log muestra "Gemini unreachable, switching to OpenAI"

**Causas comunes**:
- Red o salida IPv4/egress con problemas hacia Gemini
- `GEMINI_API_KEY` invalido o expirado
- Servicio de Gemini temporalmente caido

**Accion**: El bot funciona normalmente con OpenAI. Se auto-recupera en el siguiente restart.

### Mensajes duplicados

**Sintoma**: Bot responde dos veces al mismo mensaje

**Causas comunes**:
- Evolution API reenvia webhook (timeout en ACK)
- BoundedOrderedSet lleno (eviccion FIFO)

**Accion**:
- Verificar que el webhook retorna 200 rapido (< 1s)
- Revisar tamano de `processed_message_ids` en logs

### Bot responde a mensajes humanos

**Sintoma**: Bot interfiere cuando un asesor esta atendiendo

**Causas comunes**:
- `TEAM_NUMBERS` no configurado
- Mensaje humano no detectado por heuristicas
- Timer de silencio expiro (60 min default)

**Accion**:
- Configurar `TEAM_NUMBERS` con los numeros de los asesores
- Ajustar `AUTO_REACTIVATE_MINUTES` si 60 min no es suficiente

### Google Sheets 403

**Sintoma**: Inventario no se actualiza

**Causa**: La URL de Google Sheets no es publica

**Accion**:
1. Ir a la hoja en Google Sheets
2. Archivo -> Compartir -> "Cualquier persona con el enlace"
3. Usar URL de exportacion CSV: `https://docs.google.com/spreadsheets/d/{ID}/export?format=csv`

### Monday.com errores

**Error 401**: API key invalido -> Regenerar en Monday.com

**Error 404**: Board ID incorrecto -> Verificar `MONDAY_BOARD_ID`

**Dropdown no actualiza**: El label no coincide exactamente con los valores configurados en el tablero

**Lead duplicado**: Verificar que `MONDAY_DEDUPE_COLUMN_ID` apunta a la columna correcta

### Mensajes de audio no se transcriben

1. Verificar `OPENAI_API_KEY` (Whisper usa OpenAI)
2. Verificar que el archivo de audio es accesible desde Evolution API
3. Revisar logs para errores de transcripcion

### Imagenes no se analizan

1. Verificar `GEMINI_API_KEY` o `OPENAI_API_KEY`
2. Verificar que la imagen se descarga correctamente de Evolution API
3. Revisar logs para errores de Vision API

## Operaciones de Emergencia

### Reiniciar el bot
En Google Cloud Run: desplegar una nueva revision o relanzar desde el flujo de Cloud Build

### Silenciar bot para un numero especifico
No hay endpoint dedicado. El bot se silencia automaticamente cuando detecta intervencion humana. Se reactiva despues de `AUTO_REACTIVATE_MINUTES`.

## Arquitectura de Red

```
Cliente WhatsApp -> Evolution API -> Webhook POST /webhook -> Tono-Bot (Cloud Run)
                                                           |
                                                           v
                                                     Gemini API (primary)
                                                     OpenAI API (fallback)
                                                     Monday.com GraphQL API
                                                           |
                                                           v
                                                     Evolution API <- Respuesta WhatsApp
```

## Limpieza Cloud SQL

Conexion desde Cloud Shell:

```bash
gcloud sql connect tono-bot-db --user=tonobot_app --database=tonobot
```

Para limpiar sesiones de prueba:

```sql
DELETE FROM conversation_sessions WHERE remote_jid LIKE '%test%';
```
