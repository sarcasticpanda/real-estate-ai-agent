# 03 · Conversation Flow

> How `agent/property_agent.py` turns a raw message into the right action. This is the most
> bug-prone part of the system, so the rules here are deliberately explicit.

## 1. Dialogue state machine

Stages live on the session (`conversation_manager.py`). Valid: `discovery`, `recommending`,
`lead_capture`, `post_lead`, `done`.

```mermaid
stateDiagram-v2
    [*] --> discovery
    discovery --> discovery: not enough info → clarify
    discovery --> recommending: has(budget+area+bhk/type) → show properties
    recommending --> recommending: refine / more / compare
    recommending --> lead_capture: STRONG intent (action word)
    discovery --> lead_capture: STRONG intent
    lead_capture --> lead_capture: missing name or phone
    lead_capture --> post_lead: name+phone captured → save lead
    lead_capture --> discovery: escape hatch (user searches instead)
    post_lead --> post_lead: cooldown (3 turns)
    post_lead --> discovery: cooldown over → new search
```

`has_enough_info()` = `has(budget OR _budget_cleared) AND has(area OR _area_cleared) AND
has(bhk OR type OR _bhk_cleared)`. The `_cleared` flags let "no budget limit" / "any BHK" satisfy a slot.

## 2. Routing pipeline (`_route`)

Order matters — each guard short-circuits. This is the actual sequence:

```mermaid
flowchart TD
    A[user message] --> B{shortlist keyword?}
    B -->|yes| B1[show shortlist] --> Z[reply]
    B -->|no| C{compare/detail keyword<br/>AND properties shown<br/>AND no action word?}
    C -->|yes| C1[compare properties] --> Z
    C -->|no| D{stage == lead_capture?}
    D -->|yes, no search kw| D1[handle lead capture] --> Z
    D -->|else| E[post_lead cooldown tick]
    E --> F[detect filter-modifying intents<br/>more / diff-area / clear-budget / any-bhk]
    F --> G[extract_intent + merge]
    G --> H[resolve location switch<br/>★ drops stale anchors]
    H --> I[sticky-clear enforcement]
    I --> J{strong intent?}
    J -->|yes| J1[stage=lead_capture, ask contact] --> Z
    J -->|no| K{more-options + recommending?}
    K -->|yes| K1[recommend, exclude shown] --> Z
    K -->|no| L{can_search?}
    L -->|yes| L1[stage=recommending, recommend] --> Z
    L -->|no| M[clarify - code picks WHAT to ask] --> Z
```

## 3. Intent extraction & the guards that keep it honest

`intent_extractor.py` calls Groq (temp=0, JSON mode), then `_postprocess()` applies **code-level guards**
that override LLM mistakes. These are load-bearing — do not remove without replacing:

| Guard | Fixes |
|-------|-------|
| Lakh/crore unit correction | "15 lakh" extracted as 15.0 → 0.15 |
| Same min==max resolution | "budget 15 lakh" → min=max=0.15 blocking everything → max only |
| Budget direction | "under X" put in min → swapped to max |
| **Hallucination drop** (`_tokens_in_message`) | LLM inventing landmark/nearby the user never typed |
| **Landmark-is-area** | area name wrongly put in `named_landmark` → moved to `area` |
| **Landmark fallback** (`_NEAR_PHRASE_RE`) | LLM missing a real place → "near phoenix united" still extracted |
| **Budget grounding** (`_HAS_DIGIT_RE`/`_BUDGET_WORD_RE`) | LLM re-emitting a history budget on a no-budget turn → creates `min==max`, filters out everything cheaper |
| **bhk sanity (1–10)** | nonsense like "0 BHK Shop" dropped |
| **Strong needs action word** (`_ACTION_WORDS_RE`) | "villa"/"yes" misclassified as strong → downgraded to soft |
| Area scan from message | catches areas the LLM missed |

### Lead intent levels
- **none** — searching or answering a question (most messages, incl. bare "villa", "2 BHK", area names)
- **soft** — engaged: "this looks good", "yes", "is it negotiable?" → show + nudge, track liked property
- **strong** — explicit action word (visit/book/schedule/contact) → lead capture

## 4. ★ The location-group model (the bug we kept hitting)

`area`, `named_landmark`, and `nearby` are **one logical group**: the buyer's current location focus.
They must NOT persist independently, or a stale anchor silently filters out every later search.

**The failure it prevents** (real reproduction):
```
"2 BHK in Alambagh"        → area=Alambagh
"near Sahara Hospital"     → landmark=Sahara Hospital, nearby=[hospital], area cleared
"what about Gomti Nagar"   → area=Gomti Nagar  ... but landmark STILL Sahara Hospital ❌
                              → searches Gomti Nagar properties near a Gomti landmark = wrong/empty
```

**The rule** (`_resolve_location_switch`), fires only when the message carries a location signal:

```mermaid
flowchart LR
    M[message has location signal?] -->|no| K[keep group as-is<br/>pure refinement]
    M -->|yes| A{new explicit AREA?}
    A -->|yes| A1[area = new<br/>drop stale landmark]
    A -->|no| B{broadening or<br/>landmark-without-area?}
    B -->|yes| B1[area = null]
    B -->|no| B2[keep area]
    A1 & B1 & B2 --> C{new LANDMARK?}
    C -->|yes| C1[landmark = new]
    C -->|no| C2[landmark = null<br/>drop stale]
    C1 & C2 --> D[nearby = this message's list<br/>drop stale]
```

Only values **literally present in the current message** count (history re-extraction by the LLM is
ignored), mirroring the sticky-clear philosophy. Verified cases:

| Turn | area | landmark | nearby | ✓ |
|------|------|----------|--------|---|
| "2 BHK in Alambagh" | Alambagh | — | — | anchor set |
| "in Hazratganj instead" | Hazratganj | — | — | switched |
| "anything near Sahara Hospital" | — | Sahara Hospital | [hospital] | landmark anchor |
| "what about Gomti Nagar" | Gomti Nagar | **—** | **[]** | **stale dropped** |
| "under 40 lakh" | Gomti Nagar | — | — | refinement keeps loc |
| "near metro" | Gomti Nagar | — | [metro] | refinement adds nearby |

## 5. Sticky-clear enforcement

Separate from the location group: when a user explicitly clears a slot ("no budget limit", "any BHK",
"other areas"), a `_*_cleared` flag is set. The LLM sees history and may re-extract the old value — the
flag is lifted **only** if the current message literally contains the relevant keyword
(a price+unit, a BHK/type word, or a known area name). Otherwise the slot stays cleared.

## 6. Recommendation & honesty notes (`_recommend`)

After retrieval, before replying, the code computes an **availability note** (highest priority first):
1. **Landmark not found** — geocoding failed → "couldn't pinpoint X, here's best available"
2. Progressive fallback fired (dropped BHK, then budget, to find anything in the area)
3. **Area mismatch** — nothing in requested area → "no listings in X, showing nearby"
4. **Type mismatch** — asked villa, only flats → "no villas, closest alternatives" (flat/villa groups)
5. **Amenity mismatch** — requested amenity absent (connectivity items excluded — they're distance-filtered)

The note is injected into the LLM prompt so Riya tells the buyer the truth instead of pretending.

## 7. Comparison / detail (`_compare_properties`)

Triggers on "which is cheaper?", "compare 1 and 2", "tell me about property 3" — **only** when
properties are on screen and the message has **no action word** (so "I love #1, can I visit?" routes to
lead capture, not comparison). Answers strictly from `_last_shown_text` (no re-query, no invented data),
re-renders the same cards.

## 8. Lead capture (`_handle_lead_capture`)

Collects name+phone (channel onboarding may pre-fill). On success: save lead, notify broker, set
`post_lead` cooldown (3 turns) so the broker buttons / re-asks don't spam.

**M1 hardening (see [05_BACKLOG.md](05_BACKLOG.md)):** dedup by phone, fake-number guard, attach the
resolved liked-property id + a visit-time question, persist `intent`.
