-- Customer accounts for web login (email OTP + Google).
-- Run once in the Supabase SQL editor.

create table if not exists customers (
  id          uuid primary key default gen_random_uuid(),
  email       text unique not null,
  name        text,
  phone       text,
  google_sub  text,
  favourites  jsonb not null default '[]'::jsonb,  -- array of property ids
  created_at  timestamptz not null default now()
);

create index if not exists customers_email_idx on customers (email);
