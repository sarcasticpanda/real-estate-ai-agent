-- Real Estate AI Agent — Initial Database Schema
-- Run this in Supabase SQL Editor (Dashboard → SQL Editor → New Query)

-- Step 1: Enable pgvector extension
create extension if not exists vector;

-- ============================================================
-- PROPERTIES TABLE
-- Stores enriched property JSON + semantic embeddings
-- ============================================================
create table if not exists properties (
  id            text primary key,                -- e.g. "rag_property_PROP001"
  property_id   text unique,                     -- e.g. "PROP001"
  data          jsonb not null,                  -- full enriched property JSON
  semantic_text text,                            -- text that was embedded
  embedding     vector(384),                     -- all-MiniLM-L6-v2 (384 dims)
  area_name     text,                            -- for fast filtering
  city          text default 'Lucknow',
  bhk           int,
  price_inr     bigint,
  property_type text,
  status        text default 'available',        -- available | booked | removed
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

-- Index for vector similarity search (cosine distance)
-- Note: Run after inserting data (requires at least 1 row for ivfflat)
-- create index if not exists properties_embedding_idx
--   on properties using ivfflat (embedding vector_cosine_ops) with (lists = 10);

-- Indexes for metadata filtering
create index if not exists properties_city_idx on properties (city);
create index if not exists properties_bhk_idx on properties (bhk);
create index if not exists properties_price_idx on properties (price_inr);
create index if not exists properties_area_idx on properties (area_name);
create index if not exists properties_status_idx on properties (status);

-- ============================================================
-- BROKERS TABLE
-- ============================================================
create table if not exists brokers (
  id                text primary key,            -- e.g. "BROKER001"
  name              text not null,
  phone             text,
  email             text,
  telegram_chat_id  text,                        -- for Telegram notifications
  areas             text[],                      -- list of area names they cover
  is_active         boolean default true,
  created_at        timestamptz default now()
);

-- ============================================================
-- LEADS TABLE
-- Qualified buyer leads
-- ============================================================
create table if not exists leads (
  id              uuid primary key default gen_random_uuid(),
  session_id      text,                          -- links to user conversation
  name            text,
  phone           text,
  email           text,
  budget_min      bigint,
  budget_max      bigint,
  preferred_bhk   int,
  preferred_city  text default 'Lucknow',
  preferred_area  text,
  interested_property_id text references properties(id),
  broker_id       text references brokers(id),
  status          text default 'new',            -- new | contacted | visit_scheduled | converted | rejected
  notes           text,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);

create index if not exists leads_status_idx on leads (status);
create index if not exists leads_broker_idx on leads (broker_id);

-- ============================================================
-- SESSIONS TABLE
-- Conversation memory per user session
-- ============================================================
create table if not exists sessions (
  session_id    text primary key,               -- telegram chat_id or web session token
  platform      text default 'web',             -- telegram | web | whatsapp
  messages      jsonb default '[]',             -- list of {role, content, timestamp}
  requirements  jsonb default '{}',             -- extracted: city, bhk, budget, amenities etc.
  stage         text default 'discovery',       -- discovery | recommending | lead_capture | done
  updated_at    timestamptz default now()
);

-- ============================================================
-- MEETINGS TABLE
-- Scheduled property visits
-- ============================================================
create table if not exists meetings (
  id                uuid primary key default gen_random_uuid(),
  lead_id           uuid references leads(id),
  broker_id         text references brokers(id),
  property_id       text references properties(id),
  scheduled_at      timestamptz,
  duration_minutes  int default 60,
  status            text default 'pending',     -- pending | confirmed | completed | cancelled | rescheduled
  customer_reminded boolean default false,
  broker_reminded   boolean default false,
  visit_outcome     text,                       -- interested | negotiating | rejected | booked
  notes             text,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);

create index if not exists meetings_scheduled_idx on meetings (scheduled_at);
create index if not exists meetings_status_idx on meetings (status);

-- ============================================================
-- MATCH PROPERTIES FUNCTION
-- Called by the RAG retriever for hybrid search
-- ============================================================
create or replace function match_properties(
  query_embedding   vector(384),
  match_threshold   float    default 0.3,
  match_count       int      default 10,
  filter_city       text     default null,
  filter_max_price  bigint   default null,
  filter_min_price  bigint   default null,
  filter_bhk        int      default null,
  filter_area       text     default null
)
returns table(
  id            text,
  property_id   text,
  data          jsonb,
  semantic_text text,
  similarity    float
)
language sql stable as $$
  select
    p.id,
    p.property_id,
    p.data,
    p.semantic_text,
    1 - (p.embedding <=> query_embedding) as similarity
  from properties p
  where
    p.status = 'available'
    and (filter_city  is null or p.city       = filter_city)
    and (filter_bhk   is null or p.bhk        = filter_bhk)
    and (filter_max_price is null or p.price_inr <= filter_max_price)
    and (filter_min_price is null or p.price_inr >= filter_min_price)
    and (filter_area  is null or replace(p.area_name, ' ', '') ilike '%' || filter_area || '%')
    and 1 - (p.embedding <=> query_embedding) > match_threshold
  order by p.embedding <=> query_embedding
  limit match_count;
$$;

-- ============================================================
-- HELPER: Mark property as booked
-- ============================================================
create or replace function mark_property_booked(prop_id text)
returns void language sql as $$
  update properties set status = 'booked', updated_at = now() where id = prop_id;
$$;
