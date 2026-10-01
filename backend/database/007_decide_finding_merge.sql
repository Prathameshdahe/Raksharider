-- ============================================================================
-- 007_decide_finding_merge.sql
-- RoadWatch.AI — restore the guard 006 dropped, re-apply the column grants.
-- Idempotent. Run in the Supabase SQL editor AFTER 004, 005 and 006.
--
-- Why: 004 §3 added "a finding cannot be confirmed with zero evidence frames"
-- (docs §2.5-20). 006 §2 then re-created decide_finding from 003's body to add
-- the wrong_vehicle / plate_misread plate_history cleanup — and silently lost the
-- 004 guard. Verified on the live project on 1 Oct 2026: the deployed function has
-- the 006 cleanup but not the 004 guard. This file carries BOTH, and is the only
-- version of decide_finding that should exist from now on.
-- ============================================================================

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
  -- 004 §3 / docs §2.5-20: confirmed needs at least one evidence frame
  if p_decision = 'confirmed' and not exists (select 1 from public.evidence where finding_id = f.id) then
    raise exception 'a finding cannot be confirmed with no evidence frames' using errcode = 'P0422';
  end if;

  insert into public.finding_decisions(finding_id, case_id, cycle, reviewer_id, decision, rejection_reason, note)
  values (f.id, c.id, c.cycle, p_reviewer, p_decision, case when p_decision = 'rejected' then p_rejection_reason end, p_note);

  update public.findings
     set decision = p_decision, decided_by = p_reviewer, decided_at = now(),
         rejection_reason = case when p_decision = 'rejected' then p_rejection_reason end, note = p_note
   where id = f.id returning * into f;

  -- 006 §2 / docs §2.5-18: rejecting as wrong_vehicle or plate_misread erases the observed-layer sighting
  if p_decision = 'rejected' and p_rejection_reason in ('wrong_vehicle', 'plate_misread') then
    delete from public.plate_history where layer = 'observed' and finding_id = p_finding_id;
  end if;

  return f;
end $$;

revoke all on function public.decide_finding(uuid, text, uuid, text, text, text) from public, anon, authenticated;
grant execute on function public.decide_finding(uuid, text, uuid, text, text, text) to service_role;

-- ── 005 again (idempotent): authenticated users may only edit their own display fields ──
revoke update on public.profiles from authenticated;
grant update (full_name, phone, avatar_url, updated_at) on public.profiles to authenticated;
grant all on public.profiles to postgres, service_role;

-- ── check_idle_alerts is now called by the backend keep-alive loop (every 10 min) ──
-- pg_cron is not enabled on this project, so nothing scheduled it before. The backend's
-- service-role client needs EXECUTE; make it explicit instead of relying on defaults.
grant execute on function public.check_idle_alerts() to service_role;

-- ── Verification (run by hand after applying) ────────────────────────────────
-- select position('no evidence frames' in pg_get_functiondef('public.decide_finding'::regproc)) > 0 as has_004_guard,
--        position('wrong_vehicle'      in pg_get_functiondef('public.decide_finding'::regproc)) > 0 as has_006_cleanup;
-- -- expect: true | true
