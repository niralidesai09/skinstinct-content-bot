-- Skinstinct content bot: run once in Supabase -> SQL Editor -> New query -> Run.
-- RLS is on with no policies, so only the server (service role key) can read or write.

create table if not exists notes (
  id            bigint generated always as identity primary key,
  source        text not null,                 -- telegram / import
  chat_id       bigint,
  tg_message_id bigint,
  text          text not null,
  created_at    timestamptz not null default now(),
  score         int,                           -- 0-10
  verdict       text,                          -- pass / reject
  category      text,
  angle         text,
  reason        text,
  status        text not null default 'new',   -- new / triaged / rejected / drafted / approved / draft_rejected
  news          text,                          -- Google News item (JSON)
  unique (chat_id, tg_message_id)
);

create table if not exists drafts (
  id          bigint generated always as identity primary key,
  note_id     bigint not null references notes(id),
  text        text not null,
  news_angle  text,
  checks      text,
  created_at  timestamptz not null default now(),
  status      text not null default 'pending'  -- pending / approved / rejected / redone
);

create table if not exists voice_skill (
  name        text primary key,
  content     text not null,
  updated_at  timestamptz not null default now()
);

create table if not exists kv (k text primary key, v text);
create table if not exists processed_updates (update_id bigint primary key, created_at timestamptz not null default now());

alter table notes enable row level security;
alter table drafts enable row level security;
alter table voice_skill enable row level security;
alter table kv enable row level security;
alter table processed_updates enable row level security;
