-- Pending broker availability confirmations (two-way WhatsApp scheduling)
create table if not exists broker_confirmations (
  id               uuid primary key default gen_random_uuid(),
  lead_id          uuid references leads(id),
  meeting_id       uuid references meetings(id),
  broker_phone     text not null,
  buyer_name       text,
  buyer_phone      text,
  buyer_session_id text,
  property_id      text,
  proposed_dt      timestamptz,
  proposed_when    text,           -- human-readable "Saturday 21 Jun at 5 pm"
  status           text default 'pending', -- pending | yes | no | rescheduled
  created_at       timestamptz default now()
);

create index if not exists broker_conf_broker_phone_idx on broker_confirmations(broker_phone, status);
