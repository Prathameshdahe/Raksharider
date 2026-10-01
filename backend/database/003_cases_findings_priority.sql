-- ============================================================================
-- 003_cases_findings_priority.sql
-- RoadWatch.AI v3 — upload claim fields, priority queue, cases / findings /
-- decisions / corrections / evidence, plate history + escalation, worker
-- persist_run_result(jsonb) (contract 2.0). Implements backend/API.md §9.
-- Idempotent. Run AFTER auth_admin_setup.sql and 002_rbac_queue_notifications.sql.
-- ============================================================================

create extension if not exists pgcrypto;   -- digest() for deterministic finding ids

-- ── 1. Columns ──────────────────────────────────────────────────────────────
alter table public.videos
  add column if not exists declared_violation   text,
  add column if not exists claimed_plate        text,
  add column if not exists note                 text,
  add column if not exists recording_at         timestamptz,
  add column if not exists location_text        text,
  add column if not exists consent_at           timestamptz,
  add column if not exists priority             int not null default 0,
  add column if not exists allegation_answer    text,
  add column if not exists error_category       text,
  add column if not exists detection_video_path text,
  add column if not exists withdrawn_at         timestamptz;
create index if not exists idx_videos_queue on public.videos(status, priority desc, uploaded_at asc);

alter table public.profiles
  add column if not exists last_seen_at   timestamptz,
  add column if not exists verified_via   text,
  add column if not exists deactivated_at timestamptz;

-- ── 2. violation_policy: tiers + vehicle flags (mirror of pipeline/contract.py) ──
alter table public.violation_policy
  add column if not exists tier         text not null default 'middle',
  add column if not exists two_wheeler  boolean not null default true,
  add column if not exists four_wheeler boolean not null default true,
  add column if not exists review_only  boolean not null default false;
do $$ begin
  alter table public.violation_policy add constraint violation_policy_tier_chk check (tier in ('top','middle','minor'));
exception when duplicate_object then null; end $$;

-- pipeline/contract.py VIOLATION_POLICY is the source of truth for label / severity / enabled / tier /
-- two_wheeler / four_wheeler / review_only. Change it there first, then mirror it here (trust_delta is
-- SQL-only). erratic_driving stays disabled + review_only: from a moving dashcam the heuristic flagged
-- 21 of 23 vehicles on a real jam clip.
insert into public.violation_policy(violation_type, label, severity, trust_delta, enabled, tier, two_wheeler, four_wheeler, review_only) values
  ('wheelie',          'Wheelie (stunt riding)',   0.90, -20, true,  'top',    true,  false, true),
  ('phone_usage',      'Phone use while driving',  0.85, -15, true,  'top',    true,  true,  false),
  ('wrong_way',        'Wrong side / wrong way',   0.90, -20, false, 'top',    true,  true,  true),
  ('signal_violation', 'Red-light violation',      0.90, -20, false, 'top',    true,  true,  true),
  ('no_helmet',        'Riding without helmet',    0.70, -10, true,  'middle', true,  false, false),
  ('triple_riding',    'Triple riding',            0.75, -10, true,  'middle', true,  false, false),
  ('lane_cutting',     'Lane cutting',             0.60,  -8, false, 'middle', true,  true,  true),
  ('erratic_driving',  'Erratic driving',          0.60,  -8, false, 'middle', true,  true,  true),
  ('missing_plate',    'Missing / obscured plate', 0.50,  -5, true,  'minor',  true,  true,  true),
  ('no_seatbelt',      'No seatbelt',              0.50,  -8, false, 'minor',  false, true,  true)
on conflict (violation_type) do update set
  label = excluded.label, severity = excluded.severity, enabled = excluded.enabled, tier = excluded.tier,
  two_wheeler = excluded.two_wheeler, four_wheeler = excluded.four_wheeler, review_only = excluded.review_only;

create or replace function public.tier_priority(p_tier text) returns int
language sql immutable as $$
  select case p_tier when 'top' then 3 when 'middle' then 2 when 'minor' then 1 else 0 end;
$$;

-- ── 3. Tables ───────────────────────────────────────────────────────────────
create table if not exists public.plate_observations (
  id          bigserial primary key,
  video_id    uuid not null references public.videos(id) on delete cascade,
  run_id      text not null,
  track_id    int  not null,
  text        text not null,
  engine      text,
  confidence  real,
  frame_index int,
  timestamp   real,
  crop_path   text,
  is_valid    boolean not null default false
);
-- Repair path: a plate_observations table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.plate_observations
  add column if not exists video_id    uuid,
  add column if not exists run_id      text,
  add column if not exists track_id    int,
  add column if not exists text        text,
  add column if not exists engine      text,
  add column if not exists confidence  real,
  add column if not exists frame_index int,
  add column if not exists timestamp   real,
  add column if not exists crop_path   text,
  add column if not exists is_valid    boolean not null default false;

create index if not exists idx_plate_obs_video_track on public.plate_observations(video_id, track_id);

create table if not exists public.cases (
  id                uuid primary key default gen_random_uuid(),
  video_id          uuid not null references public.videos(id) on delete cascade,
  run_id            text not null,
  track_id          int  not null,
  vehicle_record_id uuid references public.vehicle_records(id) on delete set null,
  is_subject        boolean not null default false,
  lane              text not null default 'normal' check (lane in ('normal','not_supported')),
  status            text not null default 'pending_review'
                    check (status in ('pending_review','in_review','second_opinion','finalized','reopened','withdrawn')),
  previous_status   text,
  cycle             int  not null default 1,
  priority          int  not null default 0,
  identity_status   text,
  claimed_by        uuid,
  claimed_at        timestamptz,
  finalized_at      timestamptz,
  finalized_by      uuid,
  reopened_by       uuid,
  reopen_reason     text,
  created_at        timestamptz not null default now(),
  unique (run_id, track_id)
);
-- Repair path: a cases table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.cases
  add column if not exists video_id          uuid,
  add column if not exists run_id            text,
  add column if not exists track_id          int,
  add column if not exists vehicle_record_id uuid,
  add column if not exists is_subject        boolean not null default false,
  add column if not exists lane              text not null default 'normal',
  add column if not exists status            text not null default 'pending_review',
  add column if not exists previous_status   text,
  add column if not exists cycle             int not null default 1,
  add column if not exists priority          int not null default 0,
  add column if not exists identity_status   text,
  add column if not exists claimed_by        uuid,
  add column if not exists claimed_at        timestamptz,
  add column if not exists finalized_at      timestamptz,
  add column if not exists finalized_by      uuid,
  add column if not exists reopened_by       uuid,
  add column if not exists reopen_reason     text,
  add column if not exists created_at        timestamptz not null default now();

create index if not exists idx_cases_queue on public.cases(status, lane, priority desc, created_at asc);
create index if not exists idx_cases_claimed on public.cases(claimed_by) where status = 'in_review';

create table if not exists public.findings (
  id               text primary key,                 -- contract finding_id(run_id, track_id, violation)
  case_id          uuid references public.cases(id) on delete cascade,   -- null for tier-C-only tracks (audit only)
  video_id         uuid not null references public.videos(id) on delete cascade,
  track_id         int  not null,
  violation        text not null references public.violation_policy(violation_type),
  ai_result        text not null,
  tier             text not null check (tier in ('A','B','C')),
  confidence       real,
  agreement        real,
  evidence_frames  int,
  evaluable_frames int,
  reasoning        text,
  vlm_call_id      text,
  decision         text not null default 'pending'
                   check (decision in ('pending','confirmed','rejected','inconclusive','unverifiable')),
  decided_by       uuid,
  decided_at       timestamptz,
  rejection_reason text,
  note             text,
  created_at       timestamptz not null default now()
);
-- Repair path: a findings table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.findings
  add column if not exists case_id          uuid,
  add column if not exists video_id         uuid,
  add column if not exists track_id         int,
  add column if not exists violation        text,
  add column if not exists ai_result        text,
  add column if not exists tier             text,
  add column if not exists confidence       real,
  add column if not exists agreement        real,
  add column if not exists evidence_frames  int,
  add column if not exists evaluable_frames int,
  add column if not exists reasoning        text,
  add column if not exists vlm_call_id      text,
  add column if not exists decision         text not null default 'pending',
  add column if not exists decided_by       uuid,
  add column if not exists decided_at       timestamptz,
  add column if not exists rejection_reason text,
  add column if not exists note             text,
  add column if not exists created_at       timestamptz not null default now();

create index if not exists idx_findings_case on public.findings(case_id);
create index if not exists idx_findings_video on public.findings(video_id);

create table if not exists public.rejection_reasons (
  code  text primary key,
  label text not null,
  sort  int  not null default 0
);
-- Repair path: a rejection_reasons table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.rejection_reasons
  add column if not exists label text,
  add column if not exists sort  int not null default 0;

insert into public.rejection_reasons(code, label, sort) values
  ('helmet_worn',     'Helmet actually worn',        1),
  ('wrong_vehicle',   'Wrong vehicle identified',    2),
  ('plate_misread',   'Plate misread',               3),
  ('footage_unclear', 'Footage too unclear',         4),
  ('not_a_violation', 'Not a violation',             5),
  ('duplicate_case',  'Duplicate of another case',   6),
  ('other',           'Other (see note)',            7)
on conflict (code) do update set label = excluded.label, sort = excluded.sort;

create table if not exists public.finding_decisions (
  id               bigserial primary key,
  finding_id       text not null references public.findings(id) on delete cascade,
  case_id          uuid not null references public.cases(id) on delete cascade,
  cycle            int  not null,
  reviewer_id      uuid not null,
  decision         text not null check (decision in ('confirmed','rejected','inconclusive')),
  rejection_reason text references public.rejection_reasons(code),
  note             text,
  created_at       timestamptz not null default now()
);
-- Repair path: a finding_decisions table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.finding_decisions
  add column if not exists finding_id       text,
  add column if not exists case_id          uuid,
  add column if not exists cycle            int,
  add column if not exists reviewer_id      uuid,
  add column if not exists decision         text,
  add column if not exists rejection_reason text,
  add column if not exists note             text,
  add column if not exists created_at       timestamptz not null default now();

create index if not exists idx_finding_decisions_case on public.finding_decisions(case_id, created_at);

create table if not exists public.case_corrections (
  id              bigserial primary key,
  case_id         uuid not null references public.cases(id) on delete cascade,
  finding_id      text references public.findings(id) on delete cascade,
  field           text not null check (field in ('plate','vehicle_type','violation_label','evidence')),
  ai_value        text,
  corrected_value text,
  reason          text,
  reviewer_id     uuid not null,
  created_at      timestamptz not null default now()
);
-- Repair path: a case_corrections table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.case_corrections
  add column if not exists case_id         uuid,
  add column if not exists finding_id      text,
  add column if not exists field           text,
  add column if not exists ai_value        text,
  add column if not exists corrected_value text,
  add column if not exists reason          text,
  add column if not exists reviewer_id     uuid,
  add column if not exists created_at      timestamptz not null default now();

create index if not exists idx_case_corrections_case on public.case_corrections(case_id, created_at);

create table if not exists public.evidence (
  id          bigserial primary key,
  finding_id  text not null references public.findings(id) on delete cascade,
  case_id     uuid references public.cases(id) on delete cascade,
  track_id    int  not null,
  frame_index int,
  timestamp   real,
  blob_path   text not null,     -- {video_id}/{run_id}/{path}; never a URL
  sha256      text not null,
  created_at  timestamptz not null default now(),
  unique (finding_id, blob_path)
);
-- Repair path: a evidence table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.evidence
  add column if not exists finding_id  text,
  add column if not exists case_id     uuid,
  add column if not exists track_id    int,
  add column if not exists frame_index int,
  add column if not exists timestamp   real,
  add column if not exists blob_path   text,
  add column if not exists sha256      text,
  add column if not exists created_at  timestamptz not null default now();

create index if not exists idx_evidence_case on public.evidence(case_id);
create index if not exists idx_evidence_blob on public.evidence(blob_path);

create table if not exists public.plate_history (
  id         bigserial primary key,
  plate      text not null,
  layer      text not null check (layer in ('observed','confirmed')),
  finding_id text references public.findings(id) on delete cascade,
  case_id    uuid references public.cases(id) on delete cascade,
  video_id   uuid references public.videos(id) on delete cascade,
  violation  text,
  created_at timestamptz not null default now(),
  unique (layer, finding_id)
);
-- Repair path: a plate_history table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.plate_history
  add column if not exists plate      text,
  add column if not exists layer      text,
  add column if not exists finding_id text,
  add column if not exists case_id    uuid,
  add column if not exists video_id   uuid,
  add column if not exists violation  text,
  add column if not exists created_at timestamptz not null default now();

create index if not exists idx_plate_history_plate on public.plate_history(plate, layer);
-- backfill: the observed layer used to be written for every finding, so vehicles the AI CLEARED were
-- recorded as sightings. Only confirmed / needs_review results are observations (docs §2.7 item 27).
delete from public.plate_history h using public.findings f
 where h.finding_id = f.id and h.layer = 'observed' and f.ai_result not in ('confirmed','needs_review');

create table if not exists public.escalations (
  id               uuid primary key default gen_random_uuid(),
  plate            text not null,
  status           text not null default 'pending_approval' check (status in ('pending_approval','approved','dismissed')),
  threshold_hit    text,
  package          jsonb,
  approved_by      uuid,
  approved_at      timestamptz,
  dismissed_by     uuid,
  dismissed_reason text,
  created_at       timestamptz not null default now()
);
-- Repair path: a escalations table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.escalations
  add column if not exists plate            text,
  add column if not exists status           text not null default 'pending_approval',
  add column if not exists threshold_hit    text,
  add column if not exists package          jsonb,
  add column if not exists approved_by      uuid,
  add column if not exists approved_at      timestamptz,
  add column if not exists dismissed_by     uuid,
  add column if not exists dismissed_reason text,
  add column if not exists created_at       timestamptz not null default now();

create index if not exists idx_escalations_plate on public.escalations(plate, status);

create table if not exists public.withdrawal_requests (
  id           uuid primary key default gen_random_uuid(),
  video_id     uuid not null references public.videos(id) on delete cascade,
  requested_by uuid not null,
  status       text not null default 'pending' check (status in ('pending','accepted','declined')),
  decided_by   uuid,
  decided_at   timestamptz,
  reason       text,
  created_at   timestamptz not null default now()
);
-- Repair path: a withdrawal_requests table from an earlier build may already exist with a
-- different shape, in which case the create above is a no-op and anything below that
-- references a new column fails (42703). Add whatever is missing before it is used.
alter table public.withdrawal_requests
  add column if not exists video_id     uuid,
  add column if not exists requested_by uuid,
  add column if not exists status       text not null default 'pending',
  add column if not exists decided_by   uuid,
  add column if not exists decided_at   timestamptz,
  add column if not exists reason       text,
  add column if not exists created_at   timestamptz not null default now();

create index if not exists idx_withdrawal_video on public.withdrawal_requests(video_id, status);

drop view if exists public.plate_status;
create or replace view public.plate_status as
  with h as (
    select plate,
           count(*) filter (where layer = 'confirmed') as confirmed_count,
           count(*) filter (where layer = 'confirmed' and violation in
                            (select violation_type from public.violation_policy where tier = 'top')) as top_tier_confirmed,
           count(*) filter (where layer = 'observed') as observed_count
    from public.plate_history group by plate)
  -- docs §2.7 item 27: unconfirmed observations never prioritise, escalate or show during review —
  -- the status stays 'clean' however many there are. observed_count is exposed for model QA only.
  select h.plate,
         case when exists (select 1 from public.escalations e where e.plate = h.plate and e.status = 'approved') then 'escalated'
              when h.confirmed_count > 0 then 'watch'
              else 'clean' end as status,
         h.confirmed_count, h.top_tier_confirmed, h.observed_count
  from h;

insert into public.system_settings(key, value) values
  ('escalation_threshold_any', '3'::jsonb),
  ('escalation_threshold_top', '1'::jsonb),
  ('retention_days', '180'::jsonb),
  ('worker_lease_minutes', '120'::jsonb)   -- how long a processing lease survives without a renew_lease() heartbeat
on conflict (key) do nothing;

-- ── 4. Reviewer RPCs (SECURITY DEFINER, callable by the backend only) ───────
-- Error SQLSTATEs map to HTTP in app/utils/db.py: P0404, P0403, P0409, P0422.

create or replace function public.claim_case(p_case_id uuid, p_reviewer uuid default auth.uid())
returns public.cases language plpgsql security definer as $$
declare c public.cases; n_open int;
begin
  if p_reviewer is null then raise exception 'reviewer required' using errcode = 'P0403'; end if;
  select * into c from public.cases where id = p_case_id for update;
  if c.id is null then raise exception 'case not found' using errcode = 'P0404'; end if;
  if c.status = 'in_review' then
    if c.claimed_by = p_reviewer then return c; end if;
    raise exception 'case already claimed by another reviewer' using errcode = 'P0409';
  end if;
  if c.status not in ('pending_review','second_opinion','reopened') then
    raise exception 'case is % and cannot be claimed', c.status using errcode = 'P0409';
  end if;
  select count(*) into n_open from public.cases where claimed_by = p_reviewer and status = 'in_review';
  if n_open >= 3 then raise exception 'you already hold 3 open claims' using errcode = 'P0409'; end if;
  if c.status = 'second_opinion' and exists (
       select 1 from public.finding_decisions d
        where d.case_id = c.id and d.cycle = c.cycle and d.reviewer_id = p_reviewer) then
    raise exception 'second opinion must come from a different reviewer' using errcode = 'P0409';
  end if;
  update public.cases
     set status = 'in_review', previous_status = c.status, claimed_by = p_reviewer, claimed_at = now()
   where id = c.id returning * into c;
  return c;
end $$;

create or replace function public.release_case(p_case_id uuid, p_actor uuid default auth.uid(), p_is_admin boolean default false)
returns public.cases language plpgsql security definer as $$
declare c public.cases;
begin
  select * into c from public.cases where id = p_case_id for update;
  if c.id is null then raise exception 'case not found' using errcode = 'P0404'; end if;
  if c.status <> 'in_review' then raise exception 'case is not in review' using errcode = 'P0409'; end if;
  if c.claimed_by is distinct from p_actor and not p_is_admin then
    raise exception 'only the claimer or an admin can release this case' using errcode = 'P0403';
  end if;
  update public.cases
     set status = coalesce(previous_status, 'pending_review'), previous_status = null, claimed_by = null, claimed_at = null
   where id = c.id returning * into c;
  return c;
end $$;

create or replace function public.decide_finding(p_case_id uuid, p_finding_id text, p_reviewer uuid, p_decision text,
                                                 p_rejection_reason text default null, p_note text default null)
returns public.findings language plpgsql security definer as $$
declare c public.cases; f public.findings;
begin
  select * into c from public.cases where id = p_case_id for update;
  if c.id is null then raise exception 'case not found' using errcode = 'P0404'; end if;
  if c.status <> 'in_review' or c.claimed_by is distinct from p_reviewer then
    raise exception 'case is not in review by you' using errcode = 'P0409';
  end if;
  select * into f from public.findings where id = p_finding_id and case_id = c.id for update;
  if f.id is null then raise exception 'finding not found on this case' using errcode = 'P0404'; end if;
  if p_decision not in ('confirmed','rejected','inconclusive') then
    raise exception 'invalid decision' using errcode = 'P0422';
  end if;
  if p_decision = 'rejected' and not exists (select 1 from public.rejection_reasons where code = p_rejection_reason) then
    raise exception 'rejection_reason is required and must be a known code' using errcode = 'P0422';
  end if;
  insert into public.finding_decisions(finding_id, case_id, cycle, reviewer_id, decision, rejection_reason, note)
  values (f.id, c.id, c.cycle, p_reviewer, p_decision, case when p_decision = 'rejected' then p_rejection_reason end, p_note);
  update public.findings
     set decision = p_decision, decided_by = p_reviewer, decided_at = now(),
         rejection_reason = case when p_decision = 'rejected' then p_rejection_reason end, note = p_note
   where id = f.id returning * into f;
  return f;
end $$;

create or replace function public.finalize_case(p_case_id uuid, p_reviewer uuid default auth.uid())
returns public.cases language plpgsql security definer as $$
declare
  c public.cases; v_plate text; n_any int; n_top int; th_any int; th_top int; hit text;
begin
  select * into c from public.cases where id = p_case_id for update;
  if c.id is null then raise exception 'case not found' using errcode = 'P0404'; end if;
  if c.status <> 'in_review' or c.claimed_by is distinct from p_reviewer then
    raise exception 'case is not in review by you' using errcode = 'P0409';
  end if;
  if exists (select 1 from public.findings where case_id = c.id and decision = 'pending') then
    raise exception 'every finding needs a decision before finalizing' using errcode = 'P0422';
  end if;
  if c.previous_status = 'second_opinion' and exists (
       select 1 from public.findings f where f.case_id = c.id and f.decision = 'inconclusive'
          and not exists (select 1 from public.finding_decisions d
                           where d.finding_id = f.id and d.cycle = c.cycle and d.reviewer_id = p_reviewer)) then
    raise exception 'second reviewer must decide every inconclusive finding' using errcode = 'P0422';
  end if;

  if exists (select 1 from public.findings where case_id = c.id and decision = 'inconclusive') then
    if c.previous_status = 'second_opinion' then
      update public.findings set decision = 'unverifiable' where case_id = c.id and decision = 'inconclusive';
    else
      update public.cases
         set status = 'second_opinion', previous_status = 'in_review', claimed_by = null, claimed_at = null
       where id = c.id returning * into c;
      return c;
    end if;
  end if;

  -- plate of record: a reviewer correction always wins; the AI read counts only when identity is
  -- resolved (docs §2.4 item 12 — a 'conflict' or 'provisional' plate must never reach plate_history
  -- or escalation). No plate of record => the null check below skips history and escalation.
  select corrected_value into v_plate from public.case_corrections
   where case_id = c.id and field = 'plate' and corrected_value is not null
   order by created_at desc limit 1;
  if v_plate is null and c.identity_status = 'resolved' then
    select plate_text into v_plate from public.vehicle_records where id = c.vehicle_record_id;
  end if;
  v_plate := nullif(upper(regexp_replace(coalesce(v_plate, ''), '\s', '', 'g')), '');

  -- reopened case: the previous cycle's confirmed rows must not survive a changed decision or plate
  delete from public.plate_history where case_id = c.id and layer = 'confirmed';
  if v_plate is not null then
    insert into public.plate_history(plate, layer, finding_id, case_id, video_id, violation)
    select v_plate, 'confirmed', f.id, c.id, c.video_id, f.violation
      from public.findings f where f.case_id = c.id and f.decision = 'confirmed'
    on conflict (layer, finding_id) do nothing;

    th_any := coalesce((select (value#>>'{}')::int from public.system_settings where key = 'escalation_threshold_any'), 3);
    th_top := coalesce((select (value#>>'{}')::int from public.system_settings where key = 'escalation_threshold_top'), 1);
    select count(*),
           count(*) filter (where violation in (select violation_type from public.violation_policy where tier = 'top'))
      into n_any, n_top
      from public.plate_history where plate = v_plate and layer = 'confirmed';
    hit := case when n_top >= th_top then 'top' when n_any >= th_any then 'any' end;
    if hit is not null and not exists (
         select 1 from public.escalations where plate = v_plate and status in ('pending_approval','approved')) then
      insert into public.escalations(plate, status, threshold_hit)
      values (v_plate, 'pending_approval', hit);   -- trigger notifies admins
    end if;
  end if;

  update public.cases
     set status = 'finalized', previous_status = null, finalized_at = now(), finalized_by = p_reviewer
   where id = c.id returning * into c;
  return c;
end $$;

revoke all on function public.claim_case(uuid, uuid), public.release_case(uuid, uuid, boolean),
              public.decide_finding(uuid, text, uuid, text, text, text), public.finalize_case(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.claim_case(uuid, uuid), public.release_case(uuid, uuid, boolean),
              public.decide_finding(uuid, text, uuid, text, text, text), public.finalize_case(uuid, uuid)
  to service_role;

-- ── 5. Worker RPCs ──────────────────────────────────────────────────────────
-- return type / parameter names may differ from an earlier build
drop function if exists public.claim_next_video();
create or replace function public.claim_next_video() returns setof public.videos
language plpgsql security definer as $$
declare v public.videos; lease int;
begin
  if coalesce((select value::text = 'true' from public.system_settings where key = 'worker_paused'), false) then
    return;
  end if;

  -- Stale-lease sweep. claimed_at is the lease, NOT the start time: the worker MUST call
  -- public.renew_lease(video_id) from its heartbeat while a clip is still being processed, or a long
  -- clip is reaped and re-queued mid-run. Window = system_settings.worker_lease_minutes (default 120).
  lease := coalesce((select (value#>>'{}')::int from public.system_settings where key = 'worker_lease_minutes'), 120);
  update public.videos
     set status       = case when attempts >= 3 then 'failed' else 'unprocessed' end,
         error_reason = case when attempts >= 3 then 'Worker lease expired 3 times; giving up.' else error_reason end,
         error_category = case when attempts >= 3 then 'pipeline' else error_category end
   where status = 'processing' and claimed_at < now() - make_interval(mins => lease);

  select vd.* into v
    from public.videos vd
   where vd.status = 'unprocessed' and vd.blob_url is not null and vd.deleted_at is null
   order by vd.priority desc, vd.uploaded_at asc
   limit 1
   for update of vd skip locked;

  if v.id is null then return; end if;

  update public.videos set status = 'processing', claimed_at = now(), attempts = attempts + 1
   where id = v.id returning * into v;
  return next v;
end $$;

-- Heartbeat: the pipeline calls this every few minutes while a clip is in flight so the sweep above
-- does not reap a slow but healthy run. Returns false when the video is no longer 'processing'
-- (withdrawn, requeued or already reaped) — the worker should then abandon the run.
create or replace function public.renew_lease(p_video_id uuid) returns boolean
language plpgsql security definer as $$
begin
  update public.videos set claimed_at = now() where id = p_video_id and status = 'processing';
  return found;
end $$;

-- ONE call, ONE transaction. Raises (nothing persists) on any invariant violation.
create or replace function public.persist_run_result(p jsonb) returns jsonb
language plpgsql security definer set search_path = public, extensions as $$
declare
  v_run text := p->>'run_id';
  v_video uuid;
  vid public.videos;
  alg jsonb := p->'allegation';
  subject int := (p#>>'{allegation,subject_track_id}')::int;
  answer text := p#>>'{allegation,answer}';
  t jsonb; f jsonb; e jsonb; po jsonb;
  track_ids int[] := '{}';
  rec_ids jsonb := '{}'::jsonb;      -- track_id -> vehicle_record id
  case_ids jsonb := '{}'::jsonb;     -- track_id -> case id
  ab_tracks int[] := '{}';
  fid text; rid uuid; cid uuid; tid int; lane text; n_cases int := 0; n_findings int := 0;
  plate text;
begin
  if p->>'contract_version' is distinct from '2.0' then
    raise exception 'contract_version % is not 2.0', p->>'contract_version';
  end if;
  if v_run is null or p->>'submission_id' is null then raise exception 'run_id and submission_id are required'; end if;
  v_video := (p->>'submission_id')::uuid;
  if coalesce(jsonb_typeof(p->'vehicle_tracks'), '') <> 'array' or coalesce(jsonb_typeof(p->'findings'), '') <> 'array'
     or coalesce(jsonb_typeof(p->'evidence'), '') <> 'array' or coalesce(jsonb_typeof(alg), '') <> 'object'
     or coalesce(jsonb_typeof(p->'summary'), '') <> 'object' then
    raise exception 'package must contain vehicle_tracks[], findings[], evidence[], allegation{}, summary{}';
  end if;

  select * into vid from public.videos where id = v_video for update;
  if vid.id is null then raise exception 'video % not found', v_video; end if;
  if vid.status = 'processed' and vid.run_id = v_run then
    return jsonb_build_object('skipped', 'already_persisted', 'run_id', v_run);
  end if;
  if vid.status = 'withdrawn' then
    return jsonb_build_object('skipped', 'withdrawn', 'run_id', v_run);
  end if;

  -- A requeue makes a new run_id, so the guard above cannot dedupe it: supersede the previous run's
  -- open cases or reviewers would see every vehicle twice and escalation would double count.
  -- (The backend refuses a requeue once a case is finalized or in_review, so nothing decided is lost.)
  update public.cases set status = 'withdrawn', claimed_by = null, claimed_at = null
   where video_id = v_video and run_id <> v_run and status in ('pending_review','second_opinion','reopened');

  if answer is null or answer not in ('supported','not_supported','unobservable','ambiguous_subject','manual_review','not_declared') then
    raise exception 'bad allegation answer %', answer;
  end if;

  -- vehicle tracks -> vehicle_records (upsert on video_id, track_id)
  for t in select * from jsonb_array_elements(p->'vehicle_tracks') loop
    tid := (t->>'track_id')::int;
    if tid is null or tid < 0 then raise exception 'track_id missing/negative'; end if;
    if tid = any(track_ids) then raise exception 'duplicate track_id %', tid; end if;
    if coalesce(t->>'identity_status', 'provisional') not in ('resolved','provisional','ambiguous','conflict') then
      raise exception 'track %: invalid identity_status', tid;
    end if;
    track_ids := track_ids || tid;
    insert into public.vehicle_records
      (id, video_id, run_id, track_id, plate_text, plate_confidence, vehicle_type, frames_observed, first_seen, last_seen,
       detection_confidence, severity, confirmed_violations, review_violations, verdicts, evidence_urls, review_status, created_at)
    values
      (gen_random_uuid(), v_video, v_run, tid, t#>>'{plate,text}', coalesce((t#>>'{plate,confidence}')::real, 0),
       coalesce(t->>'vehicle_class', 'unknown'), coalesce((t->>'frames_observed')::int, 0),
       coalesce((t->>'first_seen')::real, 0), coalesce((t->>'last_seen')::real, 0),
       coalesce((t->>'detection_confidence')::real, 0), coalesce((t->>'severity')::real, 0),
       coalesce((select array_agg(x) from jsonb_array_elements_text(coalesce(t->'confirmed_violations','[]'::jsonb)) x), '{}'),
       coalesce((select array_agg(x) from jsonb_array_elements_text(coalesce(t->'review_violations','[]'::jsonb)) x), '{}'),
       coalesce(t->'verdicts', '{}'::jsonb), '[]'::jsonb,
       case when coalesce((t->>'needs_review')::boolean, false) then 'needs_review' else 'clear' end, now())
    on conflict (video_id, track_id) do update set
       run_id = excluded.run_id, plate_text = excluded.plate_text, plate_confidence = excluded.plate_confidence,
       vehicle_type = excluded.vehicle_type, frames_observed = excluded.frames_observed, first_seen = excluded.first_seen,
       last_seen = excluded.last_seen, detection_confidence = excluded.detection_confidence, severity = excluded.severity,
       confirmed_violations = excluded.confirmed_violations, review_violations = excluded.review_violations,
       verdicts = excluded.verdicts, review_status = excluded.review_status
    returning id into rid;
    rec_ids := rec_ids || jsonb_build_object(tid::text, rid);
  end loop;
  if subject is not null and not (subject = any(track_ids)) then
    raise exception 'allegation.subject_track_id % is not a vehicle track', subject;
  end if;

  -- plate observations
  -- by video, not by run: a retry's raw OCR reads replace every earlier attempt's
  delete from public.plate_observations where video_id = v_video;
  for po in select * from jsonb_array_elements(coalesce(p->'plate_observations', '[]'::jsonb)) loop
    tid := (po->>'track_id')::int;
    if not (tid = any(track_ids)) then raise exception 'plate observation references unknown track %', tid; end if;
    insert into public.plate_observations(video_id, run_id, track_id, text, engine, confidence, frame_index, timestamp, crop_path, is_valid)
    values (v_video, v_run, tid, coalesce(po->>'text', ''), po->>'engine', (po->>'confidence')::real,
            (po->>'frame_index')::int, (po->>'timestamp')::real, po->>'crop_path', coalesce((po->>'is_valid')::boolean, false));
  end loop;

  -- which tracks get a case: subject always; any track with a tier A/B finding
  for f in select * from jsonb_array_elements(p->'findings') loop
    tid := (f->>'track_id')::int;
    if not (tid = any(track_ids)) then raise exception 'finding % references unknown track %', f->>'finding_id', tid; end if;
    if not exists (select 1 from public.violation_policy where violation_type = f->>'violation') then
      raise exception 'unknown violation %', f->>'violation';
    end if;
    if f->>'result' not in ('confirmed','needs_review','observed_absent','unobservable','not_evaluated') then
      raise exception 'finding %: bad result', f->>'finding_id';
    end if;
    fid := left(encode(digest(v_run || ':' || tid || ':' || (f->>'violation'), 'sha1'), 'hex'), 20);
    if f->>'finding_id' is distinct from fid then raise exception 'finding_id % is not deterministic', f->>'finding_id'; end if;
    if f->>'tier' in ('A','B') and not (tid = any(ab_tracks)) then ab_tracks := ab_tracks || tid; end if;
  end loop;

  foreach tid in array track_ids loop
    if tid = subject or tid = any(ab_tracks) then
      -- ambiguous_subject / manual_review stay in the normal lane (docs §2.4 items 11, 13)
      lane := case when tid = subject and answer in ('not_supported','unobservable') then 'not_supported' else 'normal' end;
      insert into public.cases(video_id, run_id, track_id, vehicle_record_id, is_subject, lane, status, priority, identity_status)
      select v_video, v_run, tid, (rec_ids->>tid::text)::uuid, tid is not distinct from subject, lane, 'pending_review', vid.priority,
             vt.value->>'identity_status'
        from jsonb_array_elements(p->'vehicle_tracks') vt where (vt.value->>'track_id')::int = tid
      returning id into cid;
      case_ids := case_ids || jsonb_build_object(tid::text, cid);
      n_cases := n_cases + 1;
    end if;
  end loop;

  -- findings (+ observed plate layer) and evidence
  for f in select * from jsonb_array_elements(p->'findings') loop
    tid := (f->>'track_id')::int;
    cid := (case_ids->>tid::text)::uuid;
    insert into public.findings(id, case_id, video_id, track_id, violation, ai_result, tier, confidence, agreement,
                                evidence_frames, evaluable_frames, reasoning, vlm_call_id)
    values (f->>'finding_id', cid, v_video, tid, f->>'violation', f->>'result', f->>'tier',
            (f->>'confidence')::real, (f->>'agreement')::real, (f->>'evidence_frames')::int,
            (f->>'evaluable_frames')::int, f->>'reasoning', f->>'vlm_call_id');
    n_findings := n_findings + 1;
    -- observed layer = a sighting. A vehicle the AI cleared (observed_absent / unobservable /
    -- not_evaluated) is not a sighting and must never land in plate_history (docs §2.7 item 27).
    -- Same item: only a RESOLVED plate creates a history row — a provisional/ambiguous/conflict read
    -- is an unverified guess and would attach another vehicle's sightings to an innocent plate.
    if cid is not null and f->>'result' in ('confirmed','needs_review')
       and (select identity_status from public.cases where id = cid) = 'resolved' then
      select nullif(upper(regexp_replace(coalesce(plate_text, ''), '\s', '', 'g')), '') into plate
        from public.vehicle_records where id = (rec_ids->>tid::text)::uuid;
      if plate is not null then
        insert into public.plate_history(plate, layer, finding_id, case_id, video_id, violation)
        values (plate, 'observed', f->>'finding_id', cid, v_video, f->>'violation')
        on conflict (layer, finding_id) do nothing;
      end if;
    end if;
  end loop;

  for e in select * from jsonb_array_elements(p->'evidence') loop
    if not exists (select 1 from public.findings where id = e->>'finding_id' and video_id = v_video) then
      raise exception 'evidence % references unknown finding %', e->>'path', e->>'finding_id';
    end if;
    tid := (e->>'track_id')::int;
    if not (tid = any(track_ids)) then raise exception 'evidence references unknown track %', tid; end if;
    if coalesce(e->>'path', '') = '' or left(e->>'path', 1) = '/' or (e->>'path') like '%..%' then
      raise exception 'evidence path must be relative';
    end if;
    if length(coalesce(e->>'sha256', '')) <> 64 then raise exception 'evidence sha256 missing'; end if;
    insert into public.evidence(finding_id, case_id, track_id, frame_index, timestamp, blob_path, sha256)
    values (e->>'finding_id', (case_ids->>tid::text)::uuid, tid, (e->>'frame_index')::int, (e->>'timestamp')::real,
            v_video::text || '/' || v_run || '/' || (e->>'path'), e->>'sha256')
    on conflict (finding_id, blob_path) do nothing;
  end loop;

  update public.videos
     set status = 'processed', processed_at = now(), run_id = v_run,
         summary = (p->'summary') || jsonb_build_object(
                     'pipeline_version', p->'pipeline_version', 'model_versions', p->'model_versions',
                     'vlm_calls', coalesce(p->'vlm_calls', '[]'::jsonb), 'allegation', alg,
                     'cases_created', n_cases, 'findings', n_findings),
         allegation_answer = answer,
         detection_video_path = case when p->>'detection_video' is null then null
                                     else v_video::text || '/' || v_run || '/' || (p->>'detection_video') end,
         error_reason = null, error_category = null
   where id = v_video;

  return jsonb_build_object('run_id', v_run, 'cases', n_cases, 'findings', n_findings);
end $$;

revoke all on function public.persist_run_result(jsonb), public.claim_next_video(), public.renew_lease(uuid)
  from public, anon, authenticated;
grant execute on function public.persist_run_result(jsonb), public.claim_next_video(), public.renew_lease(uuid) to service_role;

-- ── 6. Notification triggers (API.md §8) ────────────────────────────────────
create or replace function public.on_video_status_change() returns trigger
language plpgsql security definer as $$
declare n_total int; n_flagged int;
begin
  if new.status is distinct from old.status and new.uploaded_by is not null then
    if new.status = 'unprocessed' and old.status = 'uploading' then
      insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
      values (new.uploaded_by, 'video_received', 'Your clip was received', 'It is queued for analysis.', 'info', new.id);
      if new.priority >= 3 then
        perform public.notify_role('admin', 'top_tier_submission', 'Top-tier submission',
                 format('Clip %s declares "%s".', left(new.id::text, 8), coalesce(new.declared_violation, '?')), 'warning', new.id, null);
      end if;
    elsif new.status = 'processed' then
      n_total   := coalesce((new.summary->>'total_vehicles_tracked')::int, 0);
      n_flagged := coalesce((new.summary->>'cases_created')::int, (new.summary->>'vehicles_with_violations')::int, 0);
      insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
      values (new.uploaded_by, 'video_processed', 'Your clip has been analysed',
              format('%s vehicle(s) tracked, %s case(s) awaiting human review.', n_total, n_flagged),
              case when n_flagged > 0 then 'warning' else 'info' end, new.id);
    elsif new.status = 'failed' then
      insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
      values (new.uploaded_by, 'video_failed', 'Clip could not be processed',
              coalesce(new.error_reason, 'Unknown error'), 'critical', new.id);
      perform public.notify_role('admin', 'video_failed', 'Processing failed',
               format('Clip %s failed (%s): %s', left(new.id::text, 8), coalesce(new.error_category, 'unknown'),
                      coalesce(new.error_reason, 'unknown')), 'critical', new.id, null);
    end if;
  end if;
  return new;
end $$;
drop trigger if exists trg_video_status on public.videos;
create trigger trg_video_status after update of status on public.videos
  for each row execute function public.on_video_status_change();

create or replace function public.on_case_change() returns trigger
language plpgsql security definer as $$
declare uploader uuid;
begin
  if tg_op = 'INSERT' then
    if new.priority >= 3 and new.lane = 'normal' then
      perform public.notify_role('officer', 'new_case_top', 'New top-tier case',
               format('Case %s (track %s) needs review.', left(new.id::text, 8), new.track_id), 'warning', new.video_id, null);
    end if;
    return new;
  end if;
  if new.status is distinct from old.status then
    if new.status = 'second_opinion' then
      perform public.notify_role('officer', 'second_opinion', 'Second opinion requested',
               format('Case %s has inconclusive findings and needs a second reviewer.', left(new.id::text, 8)), 'warning', new.video_id, null);
    elsif new.status = 'finalized' then
      select uploaded_by into uploader from public.videos where id = new.video_id;
      if uploader is not null then
        insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
        values (uploader, 'case_decided', 'A review on your clip is complete',
                format('Vehicle #%s: %s finding(s) confirmed.', new.track_id,
                       (select count(*) from public.findings where case_id = new.id and decision = 'confirmed')),
                'info', new.video_id);
      end if;
    end if;
  end if;
  return new;
end $$;
drop trigger if exists trg_case_change on public.cases;
create trigger trg_case_change after insert or update of status on public.cases
  for each row execute function public.on_case_change();

create or replace function public.on_escalation_insert() returns trigger
language plpgsql security definer as $$
begin
  perform public.notify_role('admin', 'escalation_pending', 'Escalation threshold reached',
           format('Plate %s hit the "%s" threshold and awaits approval.', new.plate, new.threshold_hit), 'critical', null, null);
  return new;
end $$;
drop trigger if exists trg_escalation_insert on public.escalations;
create trigger trg_escalation_insert after insert on public.escalations
  for each row execute function public.on_escalation_insert();

create or replace function public.on_withdrawal_request() returns trigger
language plpgsql security definer as $$
begin
  if tg_op = 'INSERT' then
    perform public.notify_role('admin', 'withdrawal_requested', 'Withdrawal request on a case under review',
             format('Uploader asked to withdraw clip %s.', left(new.video_id::text, 8)), 'warning', new.video_id, null);
    insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
    select distinct c.claimed_by, 'withdrawal_requested', 'Withdrawal request on your case',
           format('The uploader asked to withdraw clip %s; please answer.', left(new.video_id::text, 8)), 'warning', new.video_id
      from public.cases c where c.video_id = new.video_id and c.status = 'in_review' and c.claimed_by is not null;
  elsif new.status is distinct from old.status and new.status in ('accepted','declined') then
    insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
    values (new.requested_by, 'withdrawal_answered', 'Your withdrawal request was ' || new.status,
            coalesce(new.reason, ''), 'info', new.video_id);
  end if;
  return new;
end $$;
drop trigger if exists trg_withdrawal_request on public.withdrawal_requests;
create trigger trg_withdrawal_request after insert or update of status on public.withdrawal_requests
  for each row execute function public.on_withdrawal_request();

create or replace function public.on_role_requested() returns trigger
language plpgsql security definer as $$
begin
  if new.requested_role is not null and (tg_op = 'INSERT' or new.requested_role is distinct from old.requested_role) then
    perform public.notify_role('admin', 'reviewer_request', 'Reviewer request pending',
             format('%s asked for the %s role (badge %s).', coalesce(new.email, new.id::text), new.requested_role,
                    coalesce(new.badge_number, '-')), 'info', null, null);
  end if;
  return new;
end $$;
drop trigger if exists trg_role_requested on public.profiles;
create trigger trg_role_requested after insert or update of requested_role on public.profiles
  for each row execute function public.on_role_requested();

-- Idle alerts (worker silent 10 min, claim idle 24 h). Needs a scheduler; scheduled below if pg_cron is installed.
create or replace function public.check_idle_alerts() returns void
language plpgsql security definer as $$
declare last_seen timestamptz; c record;
begin
  last_seen := nullif(trim(both '"' from (select value::text from public.system_settings where key = 'worker_last_seen')), 'null')::timestamptz;
  if last_seen is not null and last_seen < now() - interval '10 minutes'
     and not exists (select 1 from public.notifications where kind = 'worker_silent' and created_at > now() - interval '1 hour') then
    perform public.notify_role('admin', 'worker_silent', 'AI worker silent',
             format('No heartbeat since %s.', to_char(last_seen, 'YYYY-MM-DD HH24:MI')), 'critical', null, null);
  end if;
  for c in select id, claimed_by, video_id from public.cases
            where status = 'in_review' and claimed_at < now() - interval '24 hours' loop
    if not exists (select 1 from public.notifications where kind = 'claim_idle' and recipient_id = c.claimed_by
                     and body like '%' || left(c.id::text, 8) || '%' and created_at > now() - interval '24 hours') then
      insert into public.notifications(recipient_id, kind, title, body, severity, video_id)
      values (c.claimed_by, 'claim_idle', 'Your claim is idle', format('Case %s has been open for over 24 h.', left(c.id::text, 8)), 'warning', c.video_id);
    end if;
  end loop;
end $$;
do $$ begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.unschedule(jobid) from cron.job where jobname = 'roadwatch_idle_alerts';
    perform cron.schedule('roadwatch_idle_alerts', '*/5 * * * *', 'select public.check_idle_alerts()');
  else
    raise notice 'pg_cron not installed: worker-silent / claim-idle alerts need public.check_idle_alerts() scheduled externally.';
  end if;
end $$;

-- ── 7. RLS + grants ─────────────────────────────────────────────────────────
do $$ declare p record; begin
  for p in select policyname, tablename from pg_policies
            where schemaname = 'public'
              and tablename in ('plate_observations','cases','findings','finding_decisions','case_corrections',
                                'evidence','rejection_reasons','plate_history','escalations','withdrawal_requests')
  loop execute format('drop policy if exists %I on public.%I', p.policyname, p.tablename); end loop;
end $$;

alter table public.plate_observations  enable row level security;
alter table public.cases               enable row level security;
alter table public.findings            enable row level security;
alter table public.finding_decisions   enable row level security;
alter table public.case_corrections    enable row level security;
alter table public.evidence            enable row level security;
alter table public.rejection_reasons   enable row level security;
alter table public.plate_history       enable row level security;
alter table public.escalations         enable row level security;
alter table public.withdrawal_requests enable row level security;

create or replace function public.owns_video(p_video uuid) returns boolean
language sql stable security definer as $$
  select exists (select 1 from public.videos v where v.id = p_video and v.uploaded_by = auth.uid());
$$;
grant execute on function public.owns_video(uuid) to authenticated;

-- The uploader may read their own cases so the live case list updates; claimed_by is a bare
-- uuid, not a name, and profiles RLS stops a citizen resolving it. Reviewer names and private
-- notes are never exposed here. The API remains the display path.
create policy cases_select on public.cases for select to authenticated
  using (public.is_reviewer() or public.owns_video(video_id));
-- reviewers only: findings.note is free text written by a reviewer and decided_by is the
-- reviewer's identity, both of which the API deliberately strips for citizens (uploads.py
-- returns note_public). An owns_video() branch here would hand the uploader the raw row
-- through PostgREST and undo that. Citizens see findings through GET /videos/{id} and are
-- told about decisions by notification; nothing in the frontend reads this table directly.
create policy findings_select on public.findings for select to authenticated
  using (public.is_reviewer());
-- reviewers only: citizens never receive evidence frames (docs §2.6 items 24-25), so the uploader has
-- no legitimate direct read either — an owns_video() branch here would hand them the blob paths the API strips.
create policy evidence_select on public.evidence for select to authenticated using (public.is_reviewer());
create policy plate_obs_select on public.plate_observations for select to authenticated using (public.is_reviewer());
create policy decisions_select on public.finding_decisions for select to authenticated using (public.is_reviewer());
create policy corrections_select on public.case_corrections for select to authenticated using (public.is_reviewer());
create policy reasons_select on public.rejection_reasons for select to authenticated using (true);
create policy plate_history_select on public.plate_history for select to authenticated using (public.is_reviewer());
create policy escalations_select on public.escalations for select to authenticated using (public.is_admin());
create policy withdrawals_select on public.withdrawal_requests for select to authenticated
  using (public.is_reviewer() or requested_by = auth.uid());

-- humans: read only. Every write goes through the backend (service_role) or an RPC.
revoke all on public.plate_observations, public.cases, public.findings, public.finding_decisions, public.case_corrections,
              public.evidence, public.rejection_reasons, public.plate_history, public.escalations, public.withdrawal_requests,
              public.plate_status
  from anon, authenticated;
grant select on public.plate_observations, public.cases, public.findings, public.finding_decisions, public.case_corrections,
                public.evidence, public.rejection_reasons, public.plate_history, public.escalations, public.withdrawal_requests
  to authenticated;   -- plate_status deliberately omitted: plain view, would bypass plate_history RLS for citizens
grant all on public.plate_observations, public.cases, public.findings, public.finding_decisions, public.case_corrections,
             public.evidence, public.rejection_reasons, public.plate_history, public.escalations, public.withdrawal_requests
  to service_role;
grant select on public.plate_status to service_role;
grant usage, select on all sequences in schema public to service_role;

-- worker: the two RPCs, read videos (+ mark failed), heartbeat. No direct writes to result tables any more.
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'drivetrust_ai_worker') then
    revoke insert, update, delete on public.vehicle_records, public.violations from drivetrust_ai_worker;
    grant select, update on public.videos to drivetrust_ai_worker;
    grant select, update on public.system_settings to drivetrust_ai_worker;
    grant select on public.violation_policy to drivetrust_ai_worker;
    grant execute on function public.claim_next_video(), public.persist_run_result(jsonb),
                             public.renew_lease(uuid) to drivetrust_ai_worker;
  else
    raise notice 'Role drivetrust_ai_worker does not exist; worker grants skipped.';
  end if;
end $$;

-- ── 8. Realtime ─────────────────────────────────────────────────────────────
do $$ begin
  begin alter publication supabase_realtime add table public.cases;    exception when duplicate_object then null; end;
  begin alter publication supabase_realtime add table public.findings; exception when duplicate_object then null; end;
end $$;
