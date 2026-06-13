# 01 · System Architecture

> How the pieces fit, and how one message flows through them. All components are free-tier.

## 1. Component map

```mermaid
graph TB
    subgraph Channels
        WEB["Web Chat<br/>(FastAPI static UI)"]
        TG["Telegram Bot<br/>(python-telegram-bot)"]
        WA["WhatsApp<br/>(Meta Cloud API webhook)"]
    end

    subgraph Core["Agent Core — agent/"]
        PM["process_message()<br/>property_agent.py"]
        CM["ConversationManager<br/>conversation_manager.py"]
        IE["Intent Extractor<br/>intent_extractor.py"]
        LC["Lead Collector<br/>lead_collector.py"]
    end

    subgraph RAG["Retrieval — rag/"]
        RET["retriever.py<br/>hybrid search"]
        RANK["ranker.py<br/>composite score"]
        PR["prompts.py<br/>templates"]
    end

    subgraph Enrich["Ingestion — enrichment/ broker/"]
        UP["upload_handler.py<br/>CSV → normalized"]
        GEO["geocoder.py<br/>Nominatim"]
        POI["poi_finder.py<br/>Overpass"]
        EMB["embedding_model.py<br/>all-MiniLM-L6-v2"]
    end

    subgraph External["Free services"]
        GROQ["Groq LLaMA 3.1 8B"]
        SUPA["Supabase<br/>Postgres + pgvector"]
        N8N["n8n workflows<br/>(self-host)"]
        MAIL["Gmail SMTP"]
    end

    WEB & TG & WA --> PM
    PM --> CM
    PM --> IE --> GROQ
    PM --> RET --> RANK
    PM --> PR
    PM --> LC
    CM <--> SUPA
    RET --> SUPA
    RET --> EMB
    LC --> SUPA
    LC --> MAIL
    LC --> N8N
    LC -.-> WA

    UP --> GEO --> POI --> EMB --> SUPA
    GEO --> External
```

## 2. The three layers

| Layer | Dirs | Responsibility |
|-------|------|----------------|
| **Channels** | `interfaces/`, `api/` | Transport + platform onboarding. Thin — they only adapt I/O to `process_message()`. |
| **Agent core** | `agent/` | The brain. Dialogue state, intent, routing, lead capture. Channel-agnostic. |
| **Retrieval & data** | `rag/`, `database/`, `embeddings/` | Find matching properties; persist sessions & leads. |
| **Ingestion** | `broker/`, `enrichment/` | Turn a broker CSV into enriched, embedded, searchable listings. |

**Key design rule:** channels never contain business logic. Web onboarding is the one exception
(name→phone collected before the brain sees the message) and it lives in `property_agent.py` guarded by
`platform == "web"`, not in the API layer.

## 3. Runtime request flow (a buyer message)

```mermaid
sequenceDiagram
    participant U as Buyer
    participant CH as Channel (web/tg/wa)
    participant PM as process_message()
    participant CM as ConversationManager
    participant IE as Intent Extractor (Groq)
    participant R as Retriever (pgvector)
    participant L as LLM (Groq)
    participant DB as Supabase

    U->>CH: "2 BHK in Alambagh under 50L"
    CH->>PM: process_message(sid, text, platform)
    PM->>CM: load() session + requirements
    DB-->>CM: messages, requirements, stage
    alt web & onboarding incomplete
        PM-->>CH: ask name / phone (skip brain)
    else normal
        PM->>IE: extract_intent(text, history)
        IE->>+IE: Groq JSON → postprocess (guards)
        IE-->>PM: {area, bhk, budget, landmark, lead_level...}
        PM->>PM: merge + resolve location switch + sticky-clear
        alt enough info
            PM->>R: retrieve(query, requirements)
            R->>DB: match_properties() RPC
            DB-->>R: candidates
            R->>R: dedup + exclude + rank + connectivity filter
            R-->>PM: top-k property cards
            PM->>L: recommend prompt (+ availability note)
            L-->>PM: warm 2-3 sentence reply
            PM-->>CH: reply + property cards
        else need more
            PM->>L: clarify prompt (code picks WHAT to ask)
            L-->>PM: one warm question
            PM-->>CH: reply
        end
    end
    PM->>CM: save() updated state
    CM->>DB: upsert session
```

## 4. Lead-capture sub-flow

```mermaid
sequenceDiagram
    participant U as Buyer
    participant PM as process_message()
    participant LC as Lead Collector
    participant DB as Supabase
    participant N as Notifiers (mail/WA/n8n)

    U->>PM: "I want to visit the first one" (strong intent)
    PM->>PM: stage = lead_capture
    PM-->>U: ask name + number (privacy reassurance)
    U->>PM: "Saubhagya, 98xxxxxxxx"
    PM->>LC: extract_name_and_phone()
    LC->>LC: [M1] dedup by phone + fake-number guard
    LC->>DB: insert/upsert lead (+ liked property, visit time)
    LC->>N: notify broker (context-rich)
    PM->>PM: stage = post_lead (cooldown)
    PM-->>U: "Thank you — <consultant> will call you"
```

## 5. Channel notes

- **Web** (`api/main.py`): serves an inline HTML chat UI at `/`, `POST /chat`, shortlist endpoints,
  image upload. Onboarding (name→phone) happens up front, before the brain.
- **Telegram** (`interfaces/telegram_bot.py`): `/start` onboarding, voice-note support, inline buttons.
  Can run polling locally *or* via `/webhook/telegram` when deployed.
- **WhatsApp** (`/webhook/whatsapp` in `api/main.py`): Meta Cloud API; outbound via `notifications/`.
  Needs a permanent token (see [06_SETUP_RUNBOOK.md](06_SETUP_RUNBOOK.md)).

## 6. Why these technology choices

| Need | Choice | Why (free + good enough) |
|------|--------|--------------------------|
| LLM | Groq LLaMA 3.1 8B Instant | 14.4k req/day free, fast; temp=0 for extraction |
| Embeddings | all-MiniLM-L6-v2 (local) | 384-dim, runs on CPU, no API cost |
| Vector DB | Supabase pgvector + ivfflat | free 500MB, SQL filters + cosine in one query |
| Geocoding | Nominatim | free 1 req/s; cache in M4 |
| POI distances | Overpass | free; computed once at ingestion |
| Broker automation | n8n self-host | free, visual workflows |
| Buyer/broker msgs | Gmail SMTP + `wa.me` | free; WhatsApp Cloud API free tier for outbound |

Detailed retrieval internals → [04_RAG_PIPELINE.md](04_RAG_PIPELINE.md).
Conversation routing internals → [03_CONVERSATION_FLOW.md](03_CONVERSATION_FLOW.md).
