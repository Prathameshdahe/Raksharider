-- 009 · Live schema catch-up (1 Oct 2026)
-- Run in the Supabase SQL editor as the project owner (the worker role cannot alter tables or
-- read auth.users). Every part is idempotent; re-running is harmless.
--
-- A. videos_status_check still lists the pre-002 values ('unprocessed', 'processing', 'completed',
--    'failed'). The backend writes 'uploading' at upload init and 'withdrawn' on a citizen
--    withdrawal, and the SQL function persist_run_result has written 'processed' since 003. The
--    live constraint refuses all three with SQLSTATE 23514, so today no new clip can be
--    registered ("Failed to init video upload … violates check constraint videos_status_check")
--    and no finished AI run can be saved (clips stay in 'processing'). 'completed' remains allowed
--    for the legacy rows that already carry it; nothing writes it any more.
alter table public.videos drop constraint if exists videos_status_check;
alter table public.videos add constraint videos_status_check
  check (status in ('uploading', 'unprocessed', 'processing', 'processed', 'failed', 'withdrawn', 'completed'));

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
--    lease tracking, so the 2-hour reaper in claim_next_video never releases them. Put them back
--    in the queue: the worker re-runs each one or marks it failed with a reason.
update public.videos
   set status = 'unprocessed', claimed_at = null
 where status = 'processing' and claimed_at is null;

-- Verify. Expect: the seven values from part A · 0 · 0
select pg_get_constraintdef(oid) as videos_status_check
  from pg_constraint where conname = 'videos_status_check';
select count(*) as users_without_profile
  from auth.users u left join public.profiles p on p.id = u.id where p.id is null;
select count(*) as stuck_without_lease
  from public.videos where status = 'processing' and claimed_at is null;
