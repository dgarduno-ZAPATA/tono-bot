# CLAUDE.md - Tono-Bot AI Assistant Guide

## Project Overview

**Tono-Bot** is a WhatsApp-based chatbot for **Tractos y Max**, a commercial vehicle (truck) dealership in Tlalnepantla, Mexico. The bot's core philosophy is **"DESTRABAR" (unblock)** - removing barriers and answering customer questions to encourage dealership visits, rather than hard selling.

### Key Capabilities
- WhatsApp customer support via Evolution API
- AI-powered conversations using OpenAI GPT (gpt-4o-mini)
- Vehicle inventory management with Google Sheets integration
- Lead generation and Monday.com CRM integration
- Audio message transcription (Whisper API)
- Human handoff detection
- Conversation memory with SQLite persistence
- Photo carousel for vehicles

## Repository Structure

```
/home/user/tono-bot/
├── Dockerfile                      # Root Docker config (used by Render)
├── CLAUDE.md                       # This file
└── tono-bot/                       # Main application directory
    ├── Dockerfile                  # Alternative Docker config
    ├── requirements.txt            # Python dependencies
    ├── src/
    │   ├── main.py                 # FastAPI entry point, webhooks, state management (~770 lines)
    │   ├── conversation_logic.py   # GPT conversation handler, prompts (~880 lines)
    │   ├── inventory_service.py    # Vehicle inventory from CSV/Google Sheets (~90 lines)
    │   ├── memory_store.py         # SQLite session persistence (~60 lines)
    │   └── monday_service.py       # Monday.com CRM integration (~180 lines)
    └── data/
        └── inventory.csv           # Vehicle catalog (~37 items)
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Framework | FastAPI 0.115.0 |
| Server | Uvicorn 0.30.6 |
| Runtime | Python 3.11 |
| HTTP Client | httpx 0.27.2 (async) |
| Database | SQLite via aiosqlite 0.20.0 |
| AI | OpenAI API (gpt-4o-mini, Whisper) |
| WhatsApp | Evolution API |
| CRM | Monday.com GraphQL API |
| Data | Pandas 2.2.3, Google Sheets CSV |
| Config | Pydantic Settings 2.6.1 |
| Timezone | pytz (America/Mexico_City) |

## Environment Variables

### Required
```bash
EVOLUTION_API_URL        # Evolution API endpoint
EVOLUTION_API_KEY        # Evolution API authentication
OPENAI_API_KEY           # OpenAI API key
```

### Optional (with defaults)
```bash
EVO_INSTANCE="Tractosymax2"           # WhatsApp instance name
OPENAI_MODEL="gpt-4o-mini"            # GPT model to use
OWNER_PHONE=""                         # Owner's phone for alerts
SHEET_CSV_URL=""                       # Google Sheets CSV URL for inventory
INVENTORY_REFRESH_SECONDS=300          # Inventory cache TTL
SQLITE_PATH="/app/tono-bot/db/memory.db"  # SQLite database path
TEAM_NUMBERS=""                        # Comma-separated handoff numbers
AUTO_REACTIVATE_MINUTES=60             # Bot silence duration after human detection
HUMAN_DETECTION_WINDOW_SECONDS=3       # Time window for human detection
MONDAY_API_KEY=""                      # Monday.com API key
MONDAY_BOARD_ID=""                     # Monday.com board ID
MONDAY_DEDUPE_COLUMN_ID=""             # Monday.com dedup column
MONDAY_LAST_MSG_ID_COLUMN_ID=""        # Monday.com message tracking column
MONDAY_PHONE_REAL_COLUMN_ID=""         # Monday.com phone column
MONDAY_STAGE_COLUMN_ID=""              # Monday.com funnel stage column (STATUS type)
```

## Sales Funnel System

The bot automatically tracks leads through a 4-stage sales funnel in Monday.com:

| Stage | Trigger | What it means |
|-------|---------|---------------|
| `MENSAJE` | First contact | Client sent initial message |
| `ENGANCHE` | Turn > 1 | Client is actively responding |
| `INTENCION` | Model mentioned | Client interested in specific vehicle |
| `CALIFICADO` | Appointment confirmed | Ready to visit dealership |

### How it works
1. Lead is created in Monday.com when client reaches `ENGANCHE` (responds to bot)
2. Stage updates automatically as conversation progresses
3. Notes are added at each stage transition with relevant details
4. Leads are captured even WITHOUT confirmed appointments (unlike before)

### Monday.com Setup
1. Create a STATUS column in your board for the funnel stages
2. Add labels: `MENSAJE`, `ENGANCHE`, `INTENCION`, `CALIFICADO`
3. Set `MONDAY_STAGE_COLUMN_ID` to the column ID

## Key Architecture Patterns

### 1. Async Everything
All I/O operations use async/await:
- `httpx.AsyncClient` for HTTP requests
- `aiosqlite` for database operations
- `AsyncOpenAI` for GPT calls
- Background task processing for webhook responses

### 2. Global State Management
`GlobalState` class in `main.py` manages runtime state:
- HTTP client connection
- Inventory service instance
- SQLite memory store
- Deduplication sets (BoundedOrderedSet with FIFO eviction)
- User silencing for human handoff

### 3. Error Handling
- Exponential backoff retry (3 attempts) for transient failures
- Graceful degradation with default messages on API errors
- Rate limit handling (429 responses)
- Sanitized logging (no API keys in logs)

### 4. Human Detection
Multi-layer heuristics to detect when a human agent takes over:
- Emoji presence in messages
- Specific human phrases
- Typing patterns and timestamps
- Message ID tracking

## Code Conventions

### Language
- Code comments: Spanish/English mix
- Variable names: Often Spanish (reflecting business domain)
- Commit messages: English
- Logging: English with emoji prefixes for visual scanning

### Style
- Type hints throughout (`Optional`, `Dict`, `List`, `Tuple`, etc.)
- Private functions prefixed with `_` (e.g., `_extract_name_from_text`)
- Pydantic models for configuration validation
- No global module variables (inject via function parameters)
- Defensive programming with null checks

### Bot Personality
- Name: "Adrian Jimenez"
- Max 2 sentences per response
- No emojis in bot messages
- Professional but natural tone
- Spanish language responses

## Development Commands

### Run Locally
```bash
cd tono-bot
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
```

### Docker Build
```bash
docker build -t tono-bot .
docker run -p 8080:8080 --env-file .env tono-bot
```

### Health Check
```bash
curl http://localhost:8080/health
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check with bot metrics |
| `/webhook` | POST | Evolution API webhook receiver |

## Key Files Reference

### main.py (Entry Point)
- `Settings` class: Pydantic configuration at line 26
- `BoundedOrderedSet`: FIFO eviction set at line 71
- `GlobalState`: Runtime state at line 92
- `lifespan()`: Startup/shutdown lifecycle
- `process_webhook()`: Main webhook handler
- Human detection logic with multiple heuristics
- Audio transcription pipeline

### conversation_logic.py (GPT Handler)
- `SYSTEM_PROMPT`: Bot personality and rules at line 38
- `handle_message()`: Main conversation entry point
- Turn tracking to prevent repetitive greetings
- Interest extraction from conversation
- Lead generation with JSON parsing
- Photo carousel state management

### inventory_service.py (Inventory)
- Dual-source loading (local CSV or Google Sheets)
- 300-second refresh caching
- Semantic formatting for GPT context

### memory_store.py (Persistence)
- SQLite async wrapper
- Phone-keyed session storage
- Upsert logic for state + context JSON

### monday_service.py (CRM)
- GraphQL mutations for lead creation
- Phone-based deduplication
- Retry logic with backoff

## Important Implementation Notes

1. **Webhook ACK**: Return 200 immediately, process in background to prevent retries

2. **Deduplication**: BoundedOrderedSet with 4000-8000 item limits prevents memory bloat

3. **Token Efficiency**: Conversation history truncated to ~4000 chars for GPT context

4. **Photo Carousel**: State tracked in context (`photo_index`, `photo_model`)

5. **Lead Generation**: Requires NAME + MODEL + CONFIRMED APPOINTMENT for CRM entry

6. **Human Typing Delay**: 5-10 second random delay simulates human response time

7. **Rate Limiting**: Respect 429 responses with exponential backoff

## Testing

No formal test suite currently. Manual testing via:
- Direct WhatsApp messages to bot instance
- `/health` endpoint monitoring
- Log inspection for errors

## Deployment

Deployed on **Render** PaaS:
- Port: 8080
- Dockerfile at repo root
- Environment variables configured in Render dashboard
- SQLite database persisted in `/app/tono-bot/db/`

## Recent Development Focus

Based on commit history:
- Conversation quality (semantic summaries, anti-repetition)
- Async migration (httpx, aiosqlite, AsyncOpenAI)
- Token optimization for GPT context
- Infrastructure reliability (retry logic, error handling)
- Dependency injection (no module-level globals)

## Common Issues & Solutions

| Issue | Solution |
|-------|----------|
| Bot responds to its own messages | Message ID deduplication in `processed_message_ids` |
| Repeated greetings | Turn count tracking, only greet on turn 1 |
| Google Sheets 403 | Check `SHEET_CSV_URL` is public CSV export link |
| Monday.com duplicates | Phone normalization and dedup search before create |
| Memory bloat | BoundedOrderedSet with FIFO eviction limits |
