# 04 · RAG & Retrieval Pipeline

> From a broker's raw CSV to a ranked, honestly-described shortlist. All steps free-tier.

## 1. Ingestion (one-time per listing)

```mermaid
flowchart LR
    CSV[Broker CSV row] --> N[normalize<br/>broker/upload_handler.py]
    N --> G[geocode address<br/>Nominatim → lat/lng]
    G --> P[POI distances<br/>Overpass → metro/hospital/<br/>school/market/railway/bus]
    P --> S[build semantic_text<br/>price units + synonyms + POIs]
    S --> E[embed 384-dim<br/>all-MiniLM-L6-v2]
    E --> DB[(Supabase<br/>properties + embedding)]
```

**Semantic text** (what gets embedded) deliberately includes redundancy so cosine match is robust:
price in every unit (lakh/lac/crore/cr), price-tier tags, area with and without spaces, and
property-type synonyms ("flat apartment home", "villa bungalow independent house").

**Connectivity** distances are computed **once at ingestion** and stored — search never pays for POI
lookups. (The one exception is a *named landmark* the buyer mentions at query time — §4.)

## 2. Query-time retrieval (`rag/retriever.py → retrieve()`)

```mermaid
flowchart TD
    Q[search query string<br/>built from requirements] --> EMB[embed query]
    REQ[requirements:<br/>city/area/bhk/budget] --> F[hard filters]
    EMB & F --> RPC["match_properties() RPC<br/>SQL filter + cosine, fetch 6×k"]
    RPC --> D[dedup by id]
    D --> X[exclude already-shown ids<br/>only if 'show more']
    X --> LM{named landmark?}
    LM -->|yes| LMD[geocode landmark live<br/>+ haversine distance per property]
    LM -->|no| RANK
    LMD --> RANK[rank_properties<br/>composite score]
    RANK --> CF[connectivity hard-filter<br/>nearby within limits]
    CF --> TOPK[top-k cards]
```

### Hard filters (in SQL)
`status='available'`, city, `bhk`, `min_price`, **`max_price × 1.25`** (buffer so near-budget homes
appear; the ranker penalizes over-budget ones rather than hiding them), area ILIKE (spaces stripped).

### Why fetch 6× candidates
We over-fetch (`max(k*6, 30)`) so there's room to dedup duplicate rows, exclude already-shown ids on
"show more", inject landmark distances, and re-rank — and still return a full k.

### Progressive fallback (in `_recommend`, when 0 results but area given)
1. drop BHK + type (keep area + budget)
2. also drop budget (area only)
Each fallback sets an honest availability note so Riya explains what it widened.

## 3. Ranking (`rag/ranker.py`)

Composite score over candidates (weights approximate; tune empirically):

| Signal | Weight | Logic |
|--------|--------|-------|
| Vector similarity | ~40% | cosine from pgvector |
| Budget fit | ~25% | within budget = full; over-budget penalized by distance |
| Property-type match | ~15% | exact=100, same group (villa/house) = 75, wrong type = 15 |
| Location/area match | ~12% | requested area exact vs nearby |
| Amenity / connectivity match | ~8% | requested amenities & nearby present |

The type-group penalty is what makes "show me villas" rank actual villas above flats even when both
match the vector query — and drives the honest "no villas, here are flats" note when none exist.

## 4. Named-landmark live distance (the "near X" feature)

When the buyer names a **specific** place ("near Sahara Hospital", "near CMS School"):
1. Geocode the landmark **at query time** (Nominatim).
2. Haversine distance from that point to every candidate's stored lat/lng.
3. Filter to `named_landmark_max_km` (default 3 km); if all filtered out, return unfiltered (don't 0-out).

**Confirmed working:** e.g. "Phoenix Mall" resolves to a real point and properties are measured against
it (~13 km). This is genuine query-time geospatial filtering, not a stored field.

### ⚠️ Accuracy guardrails (M1/M4 — partly TODO)
Nominatim can return the *wrong* "CMS School" or a city-centroid fallback, producing a confidently-wrong
distance. Planned mitigations:
- **Soften phrasing** when distance came from a live geocode: "approximately X km — please confirm exact
  location with our consultant" instead of a hard number.
- **Reject centroid fallbacks**: if the geocode resolves to Lucknow's centroid, treat as "not found".
- **Cache** geocodes in `geocode_cache` (see [02_DATA_MODEL.md](02_DATA_MODEL.md)) to respect the 1 req/s
  limit and allow one-time human correction of bad pins.

Total geocode failure is already handled honestly (`named_landmark_not_found` tag → "couldn't pinpoint X").

## 5. The query string builder (`_build_search_query`)

Builds the embedding query from accumulated requirements (not the raw message — conversational turns
like "yes" would tank similarity). Adds type synonyms, area, budget phrasing ("affordable under N lakh"),
nearby and amenities. This is why a one-word "villa" still retrieves well — the query is reconstructed
from full state.

## 6. Image handling

`to_card()` ensures Unsplash URLs carry CDN params (`?w=800&q=80&fit=crop&auto=format`) so browsers load
them without redirect. **M3 idea:** download + re-host images in Supabase Storage so brokers control them
and they're not Unsplash placeholders. Buyer feedback flags photos as the #1 trust signal — surface them
prominently before the contact ask ([05_BACKLOG.md](05_BACKLOG.md)).

## 7. Free-tier budget watch (M4)

| Resource | Limit | Mitigation |
|----------|-------|-----------|
| Groq | 14.4k req/day | 1 extract + 1–2 generate per turn; fine at MVP volume |
| Supabase | 500 MB | listings are small; embeddings dominate — ~110×384 floats is trivial |
| Nominatim | 1 req/s | cache geocodes (M4); ingestion already throttled |
| pgvector ivfflat | — | rebuild index if listing count grows ≫ current 110 |
