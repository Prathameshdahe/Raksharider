-- ============================================================================
-- 002_rbac_queue_notifications.sql
-- RoadWatch.AI — roles, queue fairness, notifications, audit log, score ledger.
-- Idempotent: safe to run repeatedly in the Supabase SQL editor.
-- Run AFTER auth_admin_setup.sql (profiles + is_admin() must exist).
-- ============================================================================

-- ── 0. Role helpers ─────────────────────────────────────────────────────────
create or replace function public.current_role_name() returns text
language sql stable security definer as $$
  select coalesce((select role from public.profiles where id = auth.uid()), 'anon');
$$;

create or replace function public.is_reviewer() returns boolean
language sql stable security definer as $$
  select public.current_role_name() in ('officer', 'admin');
$$;

grant execute on function public.current_role_name(), public.is_reviewer() to authenticated;

-- ── 1. profiles: users can never self-assign a role ─────────────────────────
alter table public.profiles add column if not exists requested_role text;
alter table public.profiles add column if not exists role_requested_at timestamptz;

-- audit_log + notifications must exist before the trigger below fires; created in §6/§7
-- (CREATE TABLE IF NOT EXISTS is ordered later in the file but functions bind at call time).

create or replace function public.guard_profile_role() returns trigger
language plpgsql security definer as $$
begin
  if new.role is distinct from old.role then
    -- The guard exists to stop a signed-in user promoting themselves through PostgREST.
    -- It must NOT block: the backend (service_role), an admin, or a direct SQL session
    -- such as the Supabase SQL editor, which has no JWT and is already privileged --
    -- otherwise bootstrapping the first admin by hand is impossible.
    if current_setting('request.jwt.claims', true) is not null
       and auth.role() is distinct from 'service_role'
       and not public.is_admin() then
      raise exception 'role can only be changed by an admin';
    end if;
    insert into public.audit_log(actor_id, actor_role, action, entity, entity_id, before, after)
    values (auth.uid(), public.current_role_name(), 'user.role_change', 'profile', old.id::text,
            jsonb_build_object('role', old.role), jsonb_build_object('role', new.role));
    insert into public.notifications(recipient_id, kind, title, body, severity)
    values (new.id, 'role_changed', 'Your role was updated',
            'Your account role is now "' || new.role || '".', 'info');
  end if;
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_guard_profile_role on public.profiles;
create trigger trg_guard_profile_role before update on public.profiles
  for each row execute function public.guard_profile_role();

-- Public signup always creates a citizen. An "officer" request is recorded for
-- admin approval instead of being granted. No hardcoded admin e-mail: bootstrap
-- the first admin with backend/app/scripts/create_admin_user.py.
create or replace function public.handle_new_user() returns trigger
language plpgsql security definer as $$
declare wanted text := lower(coalesce(new.raw_user_meta_data->>'role', 'citizen'));
begin
  insert into public.profiles (id, full_name, email, role, requested_role, role_requested_at, badge_number, avatar_url)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'full_name', new.raw_user_meta_data->>'name', split_part(new.email, '@', 1)),
    lower(new.email),
    'citizen',
    case when wanted in ('officer','admin') then wanted else null end,
    case when wanted in ('officer','admin') then now() else null end,
    new.raw_user_meta_data->>'badge_number',
    new.raw_user_meta_data->>'avatar_url'
  )
  on conflict (id) do update set
    email      = excluded.email,
    full_name  = coalesce(excluded.full_name, public.profiles.full_name),
    avatar_url = coalesce(excluded.avatar_url, public.profiles.avatar_url),
    updated_at = now();
  return new;
end $$;

drop policy if exists "Admins can update all profiles" on public.profiles;
create policy "Admins can update all profiles" on public.profiles
  for update to authenticated using (public.is_admin()) with check (public.is_admin());

-- ── 2. videos: queue metadata ───────────────────────────────────────────────
alter table public.videos
  add column if not exists uploaded_by      uuid,
  add column if not exists uploaded_at      timestamptz default now(),
  add column if not exists vehicle_type     text default 'two_wheeler',
  add column if not exists error_reason     text,
  add column if not exists processed_at     timestamptz,
  add column if not exists claimed_at       timestamptz,
  add column if not exists attempts         int not null default 0,
  add column if not exists run_id           text,
  add column if not exists summary          jsonb,
  add column if not exists file_size        bigint,
  add column if not exists deleted_at       timestamptz,
  add column if not exists deleted_by       uuid,
  add column if not exists delete_reason    text;
-- status lifecycle: uploading -> unprocessed -> processing -> processed | failed
create index if not exists idx_videos_status_uploaded on public.videos(status, uploaded_at);
create index if not exists idx_videos_uploaded_by on public.videos(uploaded_by);

-- ── 3. vehicle_records: one row per (video, track) ──────────────────────────
alter table public.vehicle_records
  add column if not exists run_id               text,
  add column if not exists plate_text           text,
  add column if not exists plate_confidence     real default 0,
  add column if not exists vehicle_type         text default 'unknown',
  add column if not exists frames_observed      int default 0,
  add column if not exists first_seen           real default 0,
  add column if not exists last_seen            real default 0,
  add column if not exists detection_confidence real default 0,
  add column if not exists severity             real default 0,
  add column if not exists confirmed_violations text[] default '{}',
  add column if not exists review_violations    text[] default '{}',
  add column if not exists verdicts             jsonb default '{}'::jsonb,
  add column if not exists evidence_urls        jsonb default '[]'::jsonb,
  add column if not exists review_status        text default 'clear',
  add column if not exists reviewed_by          uuid,
  add column if not exists reviewed_at          timestamptz,
  add column if not exists review_reason        text,
  add column if not exists corrected_plate      text,
  add column if not exists corrected_vehicle_type text,
  add column if not exists created_at           timestamptz default now();
-- review_status: clear | needs_review | confirmed | rejected
delete from public.vehicle_records a using public.vehicle_records b
  where a.video_id = b.video_id and a.track_id = b.track_id and a.ctid < b.ctid;
create unique index if not exists ux_vehicle_records_video_track
  on public.vehicle_records(video_id, track_id);
create index if not exists idx_vehicle_records_review on public.vehicle_records(review_status, created_at);

-- ── 4. violations: attached to a specific vehicle record ────────────────────
alter table public.violations
  add column if not exists video_id          uuid,
  add column if not exists vehicle_record_id uuid references public.vehicle_records(id) on delete cascade,
  add column if not exists track_id          int,
  add column if not exists status            text default 'needs_review',
  add column if not exists severity          real,
  add column if not exists evidence_urls     jsonb default '[]'::jsonb,
  add column if not exists reviewed_by       uuid,
  add column if not exists reviewed_at       timestamptz,
  add column if not exists review_reason     text;
do $$ begin
  alter table public.violations alter column report_id drop not null;
exception when others then null; end $$;
delete from public.violations a using public.violations b
  where a.vehicle_record_id is not null and a.vehicle_record_id = b.vehicle_record_id
    and a.violation_type = b.violation_type and a.ctid < b.ctid;
create unique index if not exists ux_violations_record_type
  on public.violations(vehicle_record_id, violation_type) where vehicle_record_id is not null;

-- ── 5. violation policy: severity + trust delta live in ONE place ───────────
create table if not exists public.violation_policy (
  violation_type text primary key,
  label          text not null,
  severity       real not null check (severity between 0 and 1),
  trust_delta    int  not null,
  enabled        boolean not null default true
);
-- Repair path: a violation_policy table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.violation_policy
  add column if not exists label       text,
  add column if not exists severity    real,
  add column if not exists trust_delta int,
  add column if not exists enabled     boolean not null default true;

insert into public.violation_policy(violation_type, label, severity, trust_delta, enabled) values
  ('no_helmet',        'Riding without helmet',    0.70, -10, true),
  ('triple_riding',    'Triple riding',            0.75, -10, true),
  ('phone_usage',      'Phone use while driving',  0.80, -15, true),
  ('wheelie',          'Wheelie (stunt riding)',   0.85, -20, true),
  ('erratic_driving',  'Erratic driving',          0.60,  -8, true),
  ('missing_plate',    'Missing / obscured plate', 0.50,  -5, true),
  ('signal_violation', 'Red-light violation',      0.90, -20, false),
  ('no_seatbelt',      'No seatbelt',              0.60,  -8, false),
  ('wrong_way',        'Wrong-way driving',        0.90, -20, false)
on conflict (violation_type) do nothing;

-- ── 6. notifications (fan-out: one row per recipient) ───────────────────────
create table if not exists public.notifications (
  id                uuid primary key default gen_random_uuid(),
  recipient_id      uuid not null references auth.users(id) on delete cascade,
  kind              text not null,     -- video_processed | video_failed | new_flag | review_decided | role_changed | system
  title             text not null,
  body              text,
  severity          text not null default 'info',   -- info | warning | critical
  video_id          uuid,
  vehicle_record_id uuid,
  read_at           timestamptz,
  created_at        timestamptz not null default now()
);
-- Repair path: a notifications table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.notifications
  add column if not exists recipient_id      uuid,
  add column if not exists kind              text,
  add column if not exists title             text,
  add column if not exists body              text,
  add column if not exists severity          text not null default 'info',
  add column if not exists video_id          uuid,
  add column if not exists vehicle_record_id uuid,
  add column if not exists read_at           timestamptz,
  add column if not exists created_at        timestamptz not null default now();

create index if not exists idx_notifications_recipient on public.notifications(recipient_id, read_at, created_at desc);

-- return type / parameter names may differ from an earlier build
drop function if exists public.notify_role(text, text, text, text, text, uuid, uuid);
create or replace function public.notify_role(p_role text, p_kind text, p_title text, p_body text,
                                              p_severity text default 'info',
                                              p_video uuid default null, p_record uuid default null)
returns void language sql security definer as $$
  insert into public.notifications(recipient_id, kind, title, body, severity, video_id, vehicle_record_id)
  select id, p_kind, p_title, p_body, p_severity, p_video, p_record
  from public.profiles where role = p_role or (p_role = 'officer' and role = 'admin');
$$;

-- ── 7. audit log (insert-only) ──────────────────────────────────────────────
create table if not exists public.audit_log (
  id          bigserial primary key,
  actor_id    uuid,
  actor_role  text,
  action      text not null,      -- review.confirmed | review.rejected | review.override | video.requeue | video.soft_delete | user.role_change | queue.pause | queue.retry_failed
  entity      text not null,      -- video | vehicle_record | profile | system
  entity_id   text not null,
  before      jsonb,
  after       jsonb,
  reason      text,
  created_at  timestamptz not null default now()
);
-- Repair path: a audit_log table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.audit_log
  add column if not exists actor_id   uuid,
  add column if not exists actor_role text,
  add column if not exists action     text,
  add column if not exists entity     text,
  add column if not exists entity_id  text,
  add column if not exists before     jsonb,
  add column if not exists after      jsonb,
  add column if not exists reason     text,
  add column if not exists created_at timestamptz not null default now();

create index if not exists idx_audit_entity on public.audit_log(entity, entity_id, created_at desc);
revoke update, delete on public.audit_log from authenticated, anon;

-- ── 8. trust score ledger (insert-only, written only on human confirmation) ─
create table if not exists public.score_ledger (
  id                bigserial primary key,
  plate             text not null,
  vehicle_record_id uuid references public.vehicle_records(id) on delete set null,
  violation_type    text,
  delta             int not null,
  reason            text,
  actor_id          uuid,
  reversal_of       bigint references public.score_ledger(id),
  created_at        timestamptz not null default now()
);
-- Repair path: a score_ledger table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.score_ledger
  add column if not exists plate             text,
  add column if not exists vehicle_record_id uuid,
  add column if not exists violation_type    text,
  add column if not exists delta             int,
  add column if not exists reason            text,
  add column if not exists actor_id          uuid,
  add column if not exists reversal_of       bigint,
  add column if not exists created_at        timestamptz not null default now();

create index if not exists idx_score_ledger_plate on public.score_ledger(plate, created_at desc);
revoke update, delete on public.score_ledger from authenticated, anon;

drop view if exists public.trust_scores;
create or replace view public.trust_scores as
  select plate, 100 + sum(delta) as score, count(*) as entries, max(created_at) as last_change
  from public.score_ledger group by plate;

-- ── 9. system settings (worker pause switch, heartbeat, quota) ──────────────
create table if not exists public.system_settings (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz not null default now(),
  updated_by uuid
);
-- Repair path: a system_settings table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.system_settings
  add column if not exists value      jsonb,
  add column if not exists updated_at timestamptz not null default now(),
  add column if not exists updated_by uuid;

insert into public.system_settings(key, value) values
  ('worker_paused', 'false'::jsonb),
  ('worker_last_seen', 'null'::jsonb),
  ('max_uploads_per_day', '10'::jsonb)
on conflict (key) do nothing;

-- ── 10. claim_next_video(): fair, leased, self-healing ──────────────────────
-- * Round-robin fairness: uploaders with fewer clips processed in the last 24h go first.
-- * Lease: processing rows older than 2h are reaped (retry up to 3 attempts, then failed).
-- * Pause switch honoured.
-- return type / parameter names may differ from an earlier build
drop function if exists public.claim_next_video();
create or replace function public.claim_next_video() returns setof public.videos
language plpgsql security definer as $$
declare v public.videos;
begin
  if coalesce((select value::text = 'true' from public.system_settings where key = 'worker_paused'), false) then
    return;
  end if;

  update public.videos
     set status       = case when attempts >= 3 then 'failed' else 'unprocessed' end,
         error_reason = case when attempts >= 3 then 'Worker lease expired 3 times; giving up.' else error_reason end
   where status = 'processing' and claimed_at < now() - interval '2 hours';

  select vd.* into v
    from public.videos vd
   where vd.status = 'unprocessed' and vd.blob_url is not null and vd.deleted_at is null
   order by (select count(*) from public.videos p
              where p.uploaded_by is not distinct from vd.uploaded_by
                and coalesce(p.processed_at, p.claimed_at) > now() - interval '24 hours') asc,
            vd.uploaded_at asc
   limit 1
   for update of vd skip locked;

  if v.id is null then return; end if;

  update public.videos set status = 'processing', claimed_at = now(), attempts = attempts + 1
   where id = v.id returning * into v;
  return next v;
end $$;

-- ── 11. Triggers that raise notifications + audit rows ──────────────────────
create or replace function public.on_video_status_change() returns trigger
language plpgsql security definer as $$
declare n_flagged int; n_total int;
begin
  if new.status is distinct from old.status and new.uploaded_by is not null then
    if new.status = 'processed' then
      n_total   := coalesce((new.summary->>'total_vehicles_tracked')::int, 0);
      n_flagged := coalesce((new.summary->>'vehicles_with_violations')::int, 0);
      insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
      values (new.uploaded_by, 'video_processed', 'Your clip has been analysed',
              format('%s vehicle(s) tracked, %s flagged for human review.', n_total, n_flagged),
              case when n_flagged > 0 then 'warning' else 'info' end, new.id);
      if n_flagged > 0 then
        perform public.notify_role('officer', 'new_flag', 'New clip awaiting review',
                 format('%s vehicle(s) flagged in clip %s.', n_flagged, left(new.id::text, 8)),
                 'warning', new.id, null);
      end if;
    elsif new.status = 'failed' then
      insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
      values (new.uploaded_by, 'video_failed', 'Clip could not be processed',
              coalesce(new.error_reason, 'Unknown error'), 'critical', new.id);
      perform public.notify_role('admin', 'video_failed', 'Processing failed',
               format('Clip %s failed: %s', left(new.id::text, 8), coalesce(new.error_reason, 'unknown')),
               'critical', new.id, null);
    end if;
  end if;
  return new;
end $$;
drop trigger if exists trg_video_status on public.videos;
create trigger trg_video_status after update of status on public.videos
  for each row execute function public.on_video_status_change();

create or replace function public.on_record_reviewed() returns trigger
language plpgsql security definer as $$
declare uploader uuid;
begin
  if new.review_status is distinct from old.review_status
     and new.review_status in ('confirmed', 'rejected') then
    insert into public.audit_log(actor_id, actor_role, action, entity, entity_id, before, after, reason)
    values (new.reviewed_by, (select role from public.profiles where id = new.reviewed_by),
            'review.' || new.review_status, 'vehicle_record', new.id::text,
            jsonb_build_object('review_status', old.review_status, 'plate_text', old.plate_text,
                               'corrected_plate', old.corrected_plate),
            jsonb_build_object('review_status', new.review_status, 'plate_text', new.plate_text,
                               'corrected_plate', new.corrected_plate),
            new.review_reason);
    select uploaded_by into uploader from public.videos where id = new.video_id;
    if uploader is not null then
      insert into public.notifications(recipient_id, kind, title, body, severity, video_id, vehicle_record_id)
      values (uploader, 'review_decided', 'A reviewer decided on your clip',
              format('Vehicle track %s: %s.', new.track_id, new.review_status), 'info', new.video_id, new.id);
    end if;
  end if;
  return new;
end $$;
drop trigger if exists trg_record_reviewed on public.vehicle_records;
create trigger trg_record_reviewed after update of review_status on public.vehicle_records
  for each row execute function public.on_record_reviewed();

-- ── 12. Row-level security ──────────────────────────────────────────────────
-- Drop every existing policy on these tables (anon SELECT included) and recreate.
do $$ declare p record; begin
  for p in select policyname, tablename from pg_policies
            where schemaname = 'public'
              and tablename in ('videos','vehicle_records','violations','notifications',
                                'audit_log','score_ledger','system_settings','violation_policy')
  loop
    execute format('drop policy if exists %I on public.%I', p.policyname, p.tablename);
  end loop;
end $$;

alter table public.videos           enable row level security;
alter table public.vehicle_records  enable row level security;
alter table public.violations       enable row level security;
alter table public.notifications    enable row level security;
alter table public.audit_log        enable row level security;
alter table public.score_ledger     enable row level security;
alter table public.system_settings  enable row level security;
alter table public.violation_policy enable row level security;

-- videos: citizens see their own; reviewers/admins see everything. Writes go through the backend.
create policy videos_select on public.videos for select to authenticated
  using (deleted_at is null and (uploaded_by = auth.uid() or public.is_reviewer()));

-- vehicle_records / violations: visible if you own the clip or you are a reviewer.
create policy records_select on public.vehicle_records for select to authenticated
  using (public.is_reviewer() or exists (select 1 from public.videos v where v.id = video_id and v.uploaded_by = auth.uid()));
create policy violations_select on public.violations for select to authenticated
  using (public.is_reviewer() or exists (select 1 from public.videos v where v.id = video_id and v.uploaded_by = auth.uid()));

-- notifications: own only; may mark own as read.
create policy notifications_select on public.notifications for select to authenticated using (recipient_id = auth.uid());
create policy notifications_update on public.notifications for update to authenticated
  using (recipient_id = auth.uid()) with check (recipient_id = auth.uid());

create policy audit_select on public.audit_log for select to authenticated
  using (public.is_admin() or actor_id = auth.uid());
create policy ledger_select on public.score_ledger for select to authenticated using (public.is_reviewer());
create policy settings_select on public.system_settings for select to authenticated using (true);
create policy policy_select on public.violation_policy for select to authenticated using (true);

grant select on public.videos, public.vehicle_records, public.violations, public.audit_log,
                public.score_ledger, public.system_settings, public.violation_policy,
                public.trust_scores to authenticated;
grant select, update on public.notifications to authenticated;
revoke all on public.videos, public.vehicle_records, public.violations, public.notifications,
              public.audit_log, public.score_ledger from anon;

-- ── 13. AI worker role: scoped grants, NO service key ───────────────────────
-- The worker connects directly to Postgres as drivetrust_ai_worker. It may claim
-- jobs and write AI results. It can NOT touch profiles, audit_log, score_ledger.
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'drivetrust_ai_worker') then
    grant usage on schema public to drivetrust_ai_worker;
    grant select, update on public.videos to drivetrust_ai_worker;
    grant select, insert, update, delete on public.vehicle_records to drivetrust_ai_worker;
    grant select, insert, update, delete on public.violations to drivetrust_ai_worker;
    grant select, update on public.system_settings to drivetrust_ai_worker;
    grant select on public.violation_policy to drivetrust_ai_worker;
    grant execute on function public.claim_next_video() to drivetrust_ai_worker;
    grant usage, select on all sequences in schema public to drivetrust_ai_worker;
    execute 'create policy worker_videos on public.videos for all to drivetrust_ai_worker using (true) with check (true)';
    execute 'create policy worker_records on public.vehicle_records for all to drivetrust_ai_worker using (true) with check (true)';
    execute 'create policy worker_violations on public.violations for all to drivetrust_ai_worker using (true) with check (true)';
    execute 'create policy worker_settings on public.system_settings for all to drivetrust_ai_worker using (true) with check (true)';
  else
    raise notice 'Role drivetrust_ai_worker does not exist. Create it (create role drivetrust_ai_worker login password ...) then re-run this file.';
  end if;
end $$;

-- ── 14. Realtime: dashboard subscribes to these ─────────────────────────────
do $$ begin
  begin alter publication supabase_realtime add table public.notifications;   exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.videos;          exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.vehicle_records; exception when duplicate_object then null; end;
end $$;

-- ── 15. Verification (run by hand after applying) ───────────────────────────
-- select * from public.claim_next_video();                 -- nothing when queue empty
-- set role drivetrust_ai_worker;
-- insert into public.score_ledger(plate, delta) values ('X', 1);   -- must FAIL: permission denied
-- reset role;
-- select * from public.notifications order by created_at desc limit 5;
