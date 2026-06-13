# Riya — Documentation

Industry-grade plan & specs for the Real Estate AI Agent (Lucknow). Start at the top.

| # | Doc | Read it for |
|---|-----|-------------|
| 00 | [Product Plan](00_PRODUCT_PLAN.md) | Vision, personas, roadmap (M0→M4), metrics, risks |
| 01 | [Architecture](01_ARCHITECTURE.md) | Components + request/lead flow diagrams, tech choices |
| 02 | [Data Model](02_DATA_MODEL.md) | Supabase schema, ER diagram, every table, the search RPC |
| 03 | [Conversation Flow](03_CONVERSATION_FLOW.md) | Dialog state machine, intent guards, the location-group model |
| 04 | [RAG Pipeline](04_RAG_PIPELINE.md) | Ingestion → embedding → hybrid retrieval → ranking → landmark distance |
| 05 | [Backlog](05_BACKLOG.md) | Prioritized work (buyer/analyst/owner findings), ROI-ranked |
| 06 | [Setup Runbook](06_SETUP_RUNBOOK.md) | Install, run, regression test, deploy, ops |

## How to use this

- **Building a feature?** Find it in [05_BACKLOG.md](05_BACKLOG.md) → it names the exact files.
- **Touching the conversation?** Read [03](03_CONVERSATION_FLOW.md) first — the guards are load-bearing.
- **Touching search/ranking?** Read [04](04_RAG_PIPELINE.md).
- **Setting up / deploying?** Follow [06](06_SETUP_RUNBOOK.md) top to bottom.

## Status at a glance (2026-06-13)

✅ Working & verified (function-level): hybrid RAG, multi-turn chat, intent guards, **location-switching
fix**, **villa/strong-intent fix**, property comparison, honest availability notes, lead capture +
broker notify.

🔜 Next (M1): lead dedup + qualify, attach liked property + visit time, fake-number guard, tone/privacy
polish. Then M2 broker dashboard + analytics.

> Diagrams are Mermaid — they render on GitHub and in most Markdown viewers/IDEs.
> `TASKS.md` (repo root) holds the operational deploy checklist; `FEATURE_IDEAS.md` holds raw ideation.
> This `docs/` set is the authoritative plan.
