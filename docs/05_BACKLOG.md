# 05 · Prioritized Backlog

> Synthesized from a three-persona review (Buyer · Business Analyst · Owner/Broker) plus retrieval
> correctness work. Each item: **problem → why it matters → fix (free-tier) → files**. Ordered by ROI.
> Tags: `[bug]` correctness · `[trust]` buyer · `[lead]` conversion · `[broker]` back-office · `[growth]` · `[risk]`

Legend for status: ✅ done · 🔜 next · ⬜ planned

---

## M1 — Trustworthy Core

### ✅ 1. `[bug]` Location-context switching drops stale anchors
Changing area/landmark mid-conversation left the old `named_landmark`/`nearby` stuck, silently
filtering out results. **Fixed** via `_resolve_location_switch` (location-group model,
[03_CONVERSATION_FLOW.md §4](03_CONVERSATION_FLOW.md)). Verified across a 6-turn switch chain.

### ✅ 2. `[bug]` Intent hallucination + "strong" misclassification
LLM invented landmarks ("CMS school" from "maybe alambagh") and mis-tagged bare answers ("villa") as
strong → wrong lead capture. **Fixed**: grounding guard + `_ACTION_WORDS_RE` downgrade (strong requires a
literal action word). Verified: villa→search, "can I visit"→capture.

### 🔜 3. `[lead][broker]` Lead dedup + qualification *(flagged by analyst AND owner)*
**Problem.** `create_lead` blind-inserts every time → duplicate leads, brokers notified twice. The
extracted `intent` is thrown away; no `timeline`/`platform`.
**Why.** Duplicates waste broker time and erode trust; missing qualifiers mean broker can't tell a hot
cash buyer from a browser.
**Fix.** Before insert, look up `phone` in last 24–48h → update instead of insert. Persist `intent`
(already extracted). Add one capture-time question ("moving within a month, or exploring?") → `timeline`.
**Files.** `agent/lead_collector.py`, `database/supabase_client.py`, `supabase/migrations/002_lead_quality.sql`.

### 🔜 4. `[bug][broker]` Attach the liked property + visit time to every lead
**Problem.** `_liked_property_id` only set on *soft* intent; on direct "visit property 2" the lead saves
with **no property**. No preferred visit time captured.
**Why.** Broker calls blind ("which one were you looking at?") — looks disorganized, wastes the opening.
**Fix.** Resolve "the first one"/"property 2" against `_last_shown_cards` to a concrete id at capture
time; store it. Add a one-line "weekday or weekend, morning or evening?" → `preferred_visit_time`.
**Files.** `agent/property_agent.py` (`_recommend`, `_handle_lead_capture`), `rag/prompts.py`.

### 🔜 5. `[risk]` Fake-number guard + basic rate limit
**Problem.** Phone validated for format only — `9999999999` passes. No rate limit on the web endpoint.
**Why.** Junk/spam leads pollute the pipeline and can blow WhatsApp free-tier quota.
**Fix.** Reject repeated/sequential-digit phones before save. Per-session/IP cap (in-memory or a tiny
`rate_limits` table). Optional: a WhatsApp confirmation acts as a soft reachability check.
**Files.** `agent/property_agent.py`, `agent/lead_collector.py`.

### 🔜 6. `[trust]` Tone polish — kill repetition + privacy reassurance at the ask
**Problem.** "Now, one quick thing" repeats within a single chat (reads as a bot). Privacy reassurance
exists at onboarding but **not** at the actual number-ask (backwards). Lead-saved names a vague
"consultant".
**Why.** Repetition breaks the human illusion right when trust matters; naming who'll call + a no-spam
promise is the difference between getting the number or not.
**Fix.** Rotate connectors / instruct "never reuse a connector". Move the no-spam line into
`LEAD_CAPTURE_PROMPT`; name the consultant + firm in `LEAD_SAVED_TEMPLATE`.
**Files.** `rag/prompts.py`.

### ⬜ 7. `[trust]` EMI / loan helper before the visit ask
**Problem.** Nothing addresses financing — the #1 anxiety of a first-home buyer.
**Why.** Proactive "for 50 lakh, EMI ≈ ₹40k/month over 20 yrs — want loan options?" earns huge trust and
differentiates from a broker who only wants to sell.
**Fix.** Add an indicative-EMI response variant on soft interest (simple formula, no API). Offer to
connect to a bank as a soft-lead reason.
**Files.** `rag/prompts.py`, `agent/property_agent.py`.

### ⬜ 8. `[trust]` Surface photos + map before requesting contact
**Problem.** Cards are text-described; buyer is asked for a number before seeing photos/location.
**Why.** Photos + map pin are the top "is this real?" signal; absence makes it feel like a lead harvester.
**Fix.** Ensure cards render images + a map/locality link and have Riya reference them; if images are
missing, say so honestly. Consider re-hosting images (M3).
**Files.** `api/main.py` (web UI), `rag/retriever.py` (`to_card`), `interfaces/telegram_bot.py`.

---

## M2 — Broker Back-Office

### ⬜ 9. `[broker]` Broker leads dashboard *(owner's #1 critical gap)*
**Problem.** Leads are push-only; `get_leads_for_broker()` exists but **no endpoint calls it**. Miss the
ping = lose the lead.
**Fix.** `GET /broker/leads?status=` + `POST /broker/leads/{id}/status` (wrap existing functions) + a
static HTML page (copy the inline-UI pattern from `/`) with Called/Visit/Won buttons. Protect with a
shared-secret header.
**Files.** `api/main.py`, `database/supabase_client.py`.

### ⬜ 10. `[broker][bug]` Mark listing SOLD + idempotent CSV upload
**Problem.** `mark_property_booked()` exists but is **never called** → bot keeps recommending sold flats.
CSV upload mints a new UUID per row → re-upload duplicates instead of updating.
**Fix.** `POST /broker/properties/{id}/status` + "Mark Sold" button. Derive `id` from an `external_ref`
column so re-uploads upsert. Auto-mark booked on meeting conversion.
**Files.** `api/main.py`, `broker/upload_handler.py`, `database/supabase_client.py`.

### ⬜ 11. `[lead]` Funnel analytics — `events` table
**Problem.** No analytics; can't see where buyers drop. Flying blind.
**Fix.** One `events` table; ~8 one-line inserts at stage transitions. Query conversion with SQL. Defines
all the M0 metrics in [00_PRODUCT_PLAN.md §6](00_PRODUCT_PLAN.md).
**Files.** `supabase/migrations/003_events.sql`, `agent/property_agent.py`.

### ⬜ 12. `[risk]` Geocode accuracy guardrail + cache
Soften live-geocode distance phrasing, reject centroid fallbacks, cache results. Details in
[04_RAG_PIPELINE.md §4](04_RAG_PIPELINE.md). **Files.** `rag/retriever.py`, `migrations/004_geocode_cache.sql`.

---

## M3 — Growth (free-tier)

### ⬜ 13. `[growth][lead]` Soft-lead capture — "WhatsApp these to me" *(analyst's #1 lever)*
**Problem.** Leads only captured on hard "visit" intent; the large interested-but-not-ready segment is
lost (web users can skip phone).
**Fix.** On soft interest / after N recommendations with no phone on file, offer "Want me to WhatsApp
these to you?" → phone-only capture → `create_lead(status='soft')`. Reuses existing WhatsApp notifier.
**Files.** `agent/property_agent.py`, `agent/lead_collector.py`, `rag/prompts.py`.

### ⬜ 14. `[growth][broker]` No-match → "we'll source it for you" lead
Turn an honest "no listing in X" into a high-intent backfill lead (`status='sourcing'`) routed to the
dashboard as a distinct bucket. **Files.** `agent/property_agent.py`, `rag/prompts.py`.

### ⬜ 15. `[growth]` Cold-lead re-engagement + saved-search alerts
n8n scheduled workflow: leads `status='new'` older than 2 days → one WhatsApp nudge; new matching
inventory → alert shortlisters. One message cap (no spam). **Files.** `n8n/workflows/`, `database/supabase_client.py`.

### ⬜ 16. `[growth]` Post-lead referral link
Append a `wa.me` share line to `LEAD_SAVED_TEMPLATE`; tag `?ref=` in `events.meta`. **Files.** `rag/prompts.py`.

---

## M4 — Live & Scaled

### ⬜ 17. Deploy to Railway free tier; permanent Telegram + WhatsApp webhooks; `/health`; monitoring.
Steps in [06_SETUP_RUNBOOK.md](06_SETUP_RUNBOOK.md) and existing `TASKS.md` Phase 2.

---

## Quick reference — "do next" shortlist
| # | Item | Effort | ROI |
|---|------|--------|-----|
| 3 | Lead dedup + qualify | Low-Med | ★★★★★ |
| 4 | Attach liked property + visit time | Low | ★★★★★ |
| 6 | Tone + privacy reassurance | Low | ★★★★ |
| 5 | Fake-number guard | Low | ★★★ |
| 9 | Broker dashboard | Med | ★★★★★ |
| 13 | Soft-lead WhatsApp capture | Low-Med | ★★★★★ |
