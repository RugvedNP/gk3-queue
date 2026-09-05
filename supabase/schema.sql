-- GK3 print queue - Supabase schema
-- Run this once in the Supabase SQL Editor (Dashboard -> SQL Editor -> New query).
--
-- Design note: this database holds QUEUE METADATA ONLY. Sliced files never
-- leave your LAN - they live on the daemon Mac and go straight to the printer
-- over SMB. That keeps you far under the free tier and makes staging fast.

create type job_state as enum (
  'queued',      -- in the queue, file still only on the daemon Mac
  'staged',      -- file copied to the printer, ready to go
  'printing',    -- printer is running it
  'done',
  'failed',
  'canceled'
);

create table jobs (
  id                uuid primary key default gen_random_uuid(),
  created_at        timestamptz not null default now(),

  -- identity
  name              text not null,              -- friendly name shown on the phone
  local_path        text not null,              -- absolute path on the daemon Mac
  printer_filename  text,                       -- filename once staged on the printer
  file_bytes        bigint,
  resin             text,                       -- which resin this was sliced for

  -- ordering + lifecycle
  position          double precision not null default 1000,
  state             job_state not null default 'queued',
  staged_at         timestamptz,
  started_at        timestamptz,
  finished_at       timestamptz,
  error             text,

  -- preflight (see daemon/preflight.py)
  preflight_status  text,                       -- ok | warn | block | skipped
  preflight_notes   text,
  slice_params      jsonb                       -- parsed header fields
);

-- position is a float so the phone UI can reorder by inserting between two
-- neighbours (avg of the two) without renumbering the whole table.
create index jobs_queue_idx on jobs (state, position);

-- Single-row table describing what the printer and daemon are doing right now.
create table printer (
  id              int primary key default 1,
  state           text not null default 'unknown',
  current_job_id  uuid references jobs(id) on delete set null,
  layer           int,
  total_layers    int,
  progress_pct    numeric(5,2),

  -- The safety interlock. Resin printers cannot chain prints: the build plate
  -- has to be physically cleared between jobs. The daemon refuses to start the
  -- next job until a human flips this true from the phone page.
  plate_clear     boolean not null default false,

  raw_status      text,                         -- last raw reply from the printer
  daemon_seen_at  timestamptz,                  -- heartbeat; stale = daemon is down
  updated_at      timestamptz not null default now(),
  constraint printer_singleton check (id = 1)
);

insert into printer (id) values (1);

-- Push updates to the phone page instead of making it poll.
alter publication supabase_realtime add table jobs;
alter publication supabase_realtime add table printer;

-- ---------------------------------------------------------------------------
-- Row level security
--
-- These policies give the anon key full access, which is fine for a printer on
-- your home LAN but means ANYONE HOLDING YOUR ANON KEY CAN QUEUE PRINTS.
-- Don't commit the key to a public repo. If you ever want real access control,
-- turn on Supabase Auth and replace `using (true)` with `using (auth.uid() is
-- not null)`.
-- ---------------------------------------------------------------------------
alter table jobs enable row level security;
alter table printer enable row level security;

create policy jobs_anon_all on jobs
  for all to anon using (true) with check (true);

create policy printer_anon_read on printer
  for select to anon using (true);

create policy printer_anon_update on printer
  for update to anon using (true) with check (true);
