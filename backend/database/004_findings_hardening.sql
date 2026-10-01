-- ============================================================================
-- 004_findings_hardening.sql
-- RoadWatch.AI — tier-C findings never block finalize, confirm requires
-- evidence, worker RLS policies survive a role created after 002/003 ran.
-- Idempotent. Run AFTER 002_rbac_queue_notifications.sql and
-- 003_cases_findings_priority.sql.
-- ============================================================================

-- ── 1. findings.decision gains 'not_queued' ─────────────────────────────────
-- Tier C is "report only" (docs §2.5-17): geometry alone never confirms, and a
-- track can carry tier-C findings on top of the violation that actually gave it
-- a case. Until now every finding on a case defaulted to 'pending', so a tier-C
-- finding like "phone_usage: unobservable" sat on the subject's case and
-- finalize_case's "every finding needs a decision" check blocked on it forever.
alter table public.findings drop constraint if exists findings_decision_check;
alter table public.findings add constraint findings_decision_check
  check (decision in ('pending','not_queued','confirmed','rejected','inconclusive','unverifiable'));

-- ── 2. persist_run_result: tier C is inserted already-decided ───────────────
-- Full body (Postgres has no ALTER FUNCTION for a single line): identical to
-- 003's version except the one `decision` expression at the findings insert.
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
    -- Tier C is report-only: geometry alone never confirms (docs §2.5-17). Insert it already
    -- decided so it never appears in "every finding needs a decision" and never blocks finalize.
    insert into public.findings(id, case_id, video_id, track_id, violation, ai_result, tier, confidence, agreement,
                                evidence_frames, evaluable_frames, reasoning, vlm_call_id, decision)
    values (f->>'finding_id', cid, v_video, tid, f->>'violation', f->>'result', f->>'tier',
            (f->>'confidence')::real, (f->>'agreement')::real, (f->>'evidence_frames')::int,
            (f->>'evaluable_frames')::int, f->>'reasoning', f->>'vlm_call_id',
            case when f->>'tier' = 'C' then 'not_queued' else 'pending' end);
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

revoke all on function public.persist_run_result(jsonb) from public, anon, authenticated;
grant execute on function public.persist_run_result(jsonb) to service_role;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'drivetrust_ai_worker') then
    grant execute on function public.persist_run_result(jsonb) to drivetrust_ai_worker;
  end if;
end $$;

-- ── 3. decide_finding: confirmed requires at least one evidence frame ───────
-- docs §2.5-20: "a finding cannot be confirmed with zero selected evidence frames."
-- Full body: identical to 003's version plus the one new guard.
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
  if p_decision = 'confirmed' and not exists (select 1 from public.evidence where finding_id = f.id) then
    raise exception 'a finding cannot be confirmed with no evidence frames' using errcode = 'P0422';
  end if;
  insert into public.finding_decisions(finding_id, case_id, cycle, reviewer_id, decision, rejection_reason, note)
  values (f.id, c.id, c.cycle, p_reviewer, p_decision, case when p_decision = 'rejected' then p_rejection_reason end, p_note);
  update public.findings
     set decision = p_decision, decided_by = p_reviewer, decided_at = now(),
         rejection_reason = case when p_decision = 'rejected' then p_rejection_reason end, note = p_note
   where id = f.id returning * into f;
  return f;
end $$;

revoke all on function public.decide_finding(uuid, text, uuid, text, text, text) from public, anon, authenticated;
grant execute on function public.decide_finding(uuid, text, uuid, text, text, text) to service_role;

-- ── 4. Worker RLS policies: create if the role exists NOW, regardless of ────
-- whether it existed when 002 ran. 002 §13 only created worker_videos /
-- worker_settings inside its own `if role exists` block; a GRANT alone (which
-- 003 §5 does) does not bypass RLS — a policy is required too. Without one,
-- the worker's raw UPDATEs (mark_failed, lease renewal, heartbeat in
-- worker.py) silently affect 0 rows: a failed clip sits until the lease
-- expires, then retries against the same failure.
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'drivetrust_ai_worker') then
    begin
      execute 'create policy worker_videos on public.videos for all to drivetrust_ai_worker using (true) with check (true)';
    exception when duplicate_object then null; end;
    begin
      execute 'create policy worker_settings on public.system_settings for all to drivetrust_ai_worker using (true) with check (true)';
    exception when duplicate_object then null; end;
  else
    raise notice 'Role drivetrust_ai_worker does not exist; worker RLS policies skipped.';
  end if;
end $$;

-- ── 5. Verification (run by hand after applying) ────────────────────────────
-- select policyname from pg_policies where tablename in ('videos','system_settings')
--   and 'drivetrust_ai_worker' = any(roles);                    -- expect 2 rows
-- select decision from public.findings where tier = 'C' limit 5; -- expect 'not_queued', not 'pending'
-- select public.decide_finding('<case>','<finding-with-no-evidence>', '<reviewer>', 'confirmed'); -- must raise P0422
