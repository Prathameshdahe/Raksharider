-- 009 · Live schema catch-up (1 Oct 2026)
-- Run in the Supabase SQL editor as the project owner (the worker role cannot alter tables or
-- read auth.users). Every part is idempotent; re-running is harmless.
--
-- Background: the three tables that existed before migration 002 (videos, vehicle_records,
-- evidence) kept their original CHECK constraints and column rules. Migrations 003/004 wrote
-- `create table if not exists` for evidence, which was a no-op against the existing table, and
-- never widened the two constraints. Everything below was confirmed against the live catalog.

-- A. videos_status_check still lists the pre-002 values ('unprocessed', 'processing', 'completed',
--    'failed'). The backend writes 'uploading' at upload init and 'withdrawn' on a citizen
--    withdrawal, and the SQL function persist_run_result has written 'processed' since 003. The
--    live constraint refuses all three with SQLSTATE 23514, so today no new clip can be
--    registered ("Failed to init video upload … violates check constraint videos_status_check")
--    and no finished AI run can be saved. 'completed' remains allowed for the legacy rows that
--    already carry it; nothing writes it any more.
alter table public.videos drop constraint if exists videos_status_check;
alter table public.videos add constraint videos_status_check
  check (status in ('uploading', 'unprocessed', 'processing', 'processed', 'failed', 'withdrawn', 'completed'));

-- A2. vehicle_records.review_status: persist_run_result writes 'needs_review' or 'clear' for every
--     tracked vehicle; the live constraint allows only 'pending_review', 'confirmed', 'rejected'.
alter table public.vehicle_records drop constraint if exists vehicle_records_review_status_check;
alter table public.vehicle_records add constraint vehicle_records_review_status_check
  check (review_status in ('clear', 'needs_review', 'pending_review', 'confirmed', 'rejected'));

-- A3. evidence keeps its pre-003 shape: vehicle_id and image_url are NOT NULL without defaults
--     (persist_run_result supplies neither), and the unique (finding_id, blob_path) that its
--     ON CONFLICT clause relies on was never created. Legacy rows have a null finding_id, which
--     never collides, so the index creation cannot fail.
alter table public.evidence alter column vehicle_id drop not null;
alter table public.evidence alter column image_url drop not null;
create unique index if not exists ux_evidence_finding_blob on public.evidence (finding_id, blob_path);

-- B. profiles rows for auth users created before the on_auth_user_created trigger existed.
--    Symptom: the browser's own-profile read returns 0 rows (406 with .single()), and an admin
--    cannot assign the user a role. Mirrors public.handle_new_user(): always 'citizen', an
--    officer/admin wish in user_metadata becomes a request for admin approval, never a role.
insert into public.profiles (id, email, full_name, role, requested_role, role_requested_at, badge_number, avatar_url)
select u.id,
       lower(u.email),
       coalesce(u.raw_user_meta_data->>'full_name', u.raw_user_meta_data->>'name', split_part(u.email, '@', 1)),
       'citizen',
       case when lower(u.raw_user_meta_data->>'role') in ('officer', 'admin') then lower(u.raw_user_meta_data->>'role') end,
       case when lower(u.raw_user_meta_data->>'role') in ('officer', 'admin') then now() end,
       u.raw_user_meta_data->>'badge_number',
       u.raw_user_meta_data->>'avatar_url'
from auth.users u
left join public.profiles p on p.id = u.id
where p.id is null
on conflict (id) do nothing;

-- C. (optional) Legacy clips stuck in 'processing' with no lease (claimed_at is null). They predate
--    lease tracking, so the 2-hour reaper in claim_next_video never releases them. Clips that still
--    have a stored file go back in the queue (the worker re-runs each one or marks it failed with a
--    reason); clips with no file at all are marked failed now — claim_next_video only picks rows
--    with a blob_url, so re-queueing those would leave phantom entries in the public queue depth.
update public.videos
   set status         = case when blob_url is null then 'failed' else 'unprocessed' end,
       error_reason   = case when blob_url is null then 'Legacy upload without a stored video file' else error_reason end,
       error_category = case when blob_url is null then 'download' else error_category end,
       claimed_at     = null
 where status = 'processing' and claimed_at is null;

-- Verify. Expect: the seven values from part A · the five from A2 · 1 · 0 · 0 · 0
select pg_get_constraintdef(oid) as videos_status_check
  from pg_constraint where conname = 'videos_status_check';
select pg_get_constraintdef(oid) as vehicle_records_review_status_check
  from pg_constraint where conname = 'vehicle_records_review_status_check';
select count(*) as evidence_unique_index
  from pg_indexes where schemaname = 'public' and indexname = 'ux_evidence_finding_blob';
select count(*) as evidence_not_null_legacy_columns
  from information_schema.columns
 where table_schema = 'public' and table_name = 'evidence'
   and column_name in ('vehicle_id', 'image_url') and is_nullable = 'NO';
select count(*) as users_without_profile
  from auth.users u left join public.profiles p on p.id = u.id where p.id is null;
select count(*) as stuck_without_lease
  from public.videos where status = 'processing' and claimed_at is null;
