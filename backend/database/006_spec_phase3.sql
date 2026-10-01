-- 006_spec_phase3.sql
-- -------------------------------------------------------------
-- Implements Phase 3 spec requirements from docs/PRODUCT_FLOW_DECISIONS.md:
-- 1. §2.2-5: sha256 column on videos for duplicate upload prevention
-- 2. §2.5-18: delete observed-layer plate_history on wrong_vehicle / plate_misread rejections
-- 3. §2.5-16: 48h idle auto-release in check_idle_alerts with audit log and notification

-- ── 1. Videos sha256 column ──────────────────────────────────────────────────
ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS sha256 TEXT;
CREATE INDEX IF NOT EXISTS idx_videos_sha256 ON public.videos (sha256) WHERE deleted_at IS NULL;

-- ── 2. decide_finding: delete observed plate_history on wrong_vehicle / plate_misread ──
CREATE OR REPLACE FUNCTION public.decide_finding(
    p_case_id uuid,
    p_finding_id text,
    p_reviewer uuid,
    p_decision text,
    p_rejection_reason text DEFAULT null,
    p_note text DEFAULT null
)
RETURNS public.findings LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    c public.cases;
    f public.findings;
BEGIN
    SELECT * INTO c FROM public.cases WHERE id = p_case_id FOR UPDATE;
    IF c.id IS NULL THEN RAISE EXCEPTION 'case not found' USING errcode = 'P0404'; END IF;
    IF c.status <> 'in_review' OR c.claimed_by IS DISTINCT FROM p_reviewer THEN
        RAISE EXCEPTION 'case is not in review by you' USING errcode = 'P0409';
    END IF;

    SELECT * INTO f FROM public.findings WHERE id = p_finding_id AND case_id = c.id FOR UPDATE;
    IF f.id IS NULL THEN RAISE EXCEPTION 'finding not found on this case' USING errcode = 'P0404'; END IF;

    IF p_decision NOT IN ('confirmed', 'rejected', 'inconclusive') THEN
        RAISE EXCEPTION 'invalid decision' USING errcode = 'P0422';
    END IF;
    IF p_decision = 'rejected' AND NOT EXISTS (SELECT 1 FROM public.rejection_reasons WHERE code = p_rejection_reason) THEN
        RAISE EXCEPTION 'rejection_reason is required and must be a known code' USING errcode = 'P0422';
    END IF;

    INSERT INTO public.finding_decisions(finding_id, case_id, cycle, reviewer_id, decision, rejection_reason, note)
    VALUES (f.id, c.id, c.cycle, p_reviewer, p_decision, CASE WHEN p_decision = 'rejected' THEN p_rejection_reason END, p_note);

    UPDATE public.findings
       SET decision = p_decision,
           decided_by = p_reviewer,
           decided_at = now(),
           rejection_reason = CASE WHEN p_decision = 'rejected' THEN p_rejection_reason END,
           note = p_note
     WHERE id = f.id RETURNING * INTO f;

    -- §2.5-18: rejecting as wrong_vehicle or plate_misread erases the observed-layer sighting
    IF p_decision = 'rejected' AND p_rejection_reason IN ('wrong_vehicle', 'plate_misread') THEN
        DELETE FROM public.plate_history WHERE layer = 'observed' AND finding_id = p_finding_id;
    END IF;

    RETURN f;
END $$;

-- ── 3. check_idle_alerts: 48h auto-release + 24h idle alert ──────────────────
CREATE OR REPLACE FUNCTION public.check_idle_alerts() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    last_seen timestamptz;
    c record;
    v_last_active timestamptz;
    v_prev_status text;
BEGIN
    -- 1. Check AI worker heartbeat
    last_seen := nullif(trim(both '"' from (SELECT value::text FROM public.system_settings WHERE key = 'worker_last_seen')), 'null')::timestamptz;
    IF last_seen IS NOT NULL AND last_seen < now() - interval '10 minutes'
       AND NOT EXISTS (SELECT 1 FROM public.notifications WHERE kind = 'worker_silent' AND created_at > now() - interval '1 hour') THEN
        PERFORM public.notify_role('admin', 'worker_silent', 'AI worker silent',
                 format('No heartbeat since %s.', to_char(last_seen, 'YYYY-MM-DD HH24:MI')), 'critical', null, null);
    END IF;

    -- 2. Check in_review cases for idle alerts (24h) and auto-release (48h)
    FOR c IN SELECT id, claimed_by, video_id, status, previous_status, claimed_at FROM public.cases
              WHERE status = 'in_review' AND claimed_at IS NOT NULL LOOP
        -- Compute last activity: claimed_at or latest decision/correction since claim
        v_last_active := greatest(
            c.claimed_at,
            coalesce((SELECT max(created_at) FROM public.finding_decisions WHERE case_id = c.id AND created_at >= c.claimed_at), c.claimed_at),
            coalesce((SELECT max(created_at) FROM public.case_corrections WHERE case_id = c.id AND created_at >= c.claimed_at), c.claimed_at)
        );

        -- 48h idle: auto-release back to queue
        IF v_last_active < now() - interval '48 hours' THEN
            v_prev_status := coalesce(c.previous_status, 'pending_review');
            UPDATE public.cases
               SET status = v_prev_status,
                   previous_status = null,
                   claimed_by = null,
                   claimed_at = null
             WHERE id = c.id;

            -- Write audit row: case.auto_release
            INSERT INTO public.audit_log(actor_id, actor_role, action, entity, entity_id, before, after, reason)
            VALUES (
                c.claimed_by,
                'system',
                'case.auto_release',
                'case',
                c.id::text,
                jsonb_build_object('status', 'in_review', 'claimed_by', c.claimed_by, 'claimed_at', c.claimed_at),
                jsonb_build_object('status', v_prev_status, 'claimed_by', null, 'claimed_at', null),
                'Auto-released after 48h with no reviewer activity'
            );

            -- Notify reviewer
            INSERT INTO public.notifications(recipient_id, kind, title, body, severity, video_id)
            VALUES (
                c.claimed_by,
                'claim_auto_released',
                'Claim auto-released',
                format('Case %s was automatically released after 48 hours of inactivity.', left(c.id::text, 8)),
                'warning',
                c.video_id
            );

        -- 24h idle: alert reviewer
        ELSIF v_last_active < now() - interval '24 hours' THEN
            IF NOT EXISTS (SELECT 1 FROM public.notifications WHERE kind = 'claim_idle' AND recipient_id = c.claimed_by
                             AND body LIKE '%' || left(c.id::text, 8) || '%' AND created_at > now() - interval '24 hours') THEN
                INSERT INTO public.notifications(recipient_id, kind, title, body, severity, video_id)
                VALUES (c.claimed_by, 'claim_idle', 'Your claim is idle', format('Case %s has been open for over 24 h.', left(c.id::text, 8)), 'warning', c.video_id);
            END IF;
        END IF;
    END LOOP;
END $$;
