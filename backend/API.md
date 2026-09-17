# RoadWatch.AI backend API contract (v3, 17 Sep 2026)

Implements `docs/PRODUCT_FLOW_DECISIONS.md`. Supersedes v2. Everything below is enforced in FastAPI
(`require_role`, ownership, resource state) and again by database RLS / grants.

All endpoints require `Authorization: Bearer <Supabase access_token>` unless marked public.
Role comes from `public.profiles.role` only. DB values stay `citizen | officer | admin`
(UI labels User / Reviewer / Admin). `admin` satisfies every `officer` check.
Error shape: `{"detail": {"success": false, "error": "<message>"}}`. Success: `{"success": true, "data": ...}`.

## 1. Auth (unchanged from v2 except noted)
| Method | Path | Who | Notes |
|---|---|---|---|
| POST | /auth/signup | public | always citizen; `role: "officer"` + `badge_number` → `requested_role`. |
| POST | /auth/login | public | |
| GET  | /auth/me | any | `{user_id, email, profile, portal: "user"\|"reviewer"\|"admin", nav: [...]}` |
| PUT  | /auth/profile | any | full_name / phone / avatar_url |
| POST | /auth/presence | any | heartbeat: sets `profiles.last_seen_at = now()`. Frontend calls every 60 s. Powers "users online". |

## 2. Submissions (videos table; the word "submission" in the UI)
| Method | Path | Who | Notes |
|---|---|---|---|
| POST | /videos/upload/init | citizen+ | body `{filename, content_type, size_bytes, vehicle_type: two_wheeler\|four_wheeler, declared_violation: <violation_policy key>\|null, claimed_plate?: string, note: string (10–400 chars), recording_at?: iso, location_text?: string, consent: true}`. `consent` must be true (422). `claimed_plate` normalised (uppercase, no spaces) and must pass the Indian format or 422. `declared_violation` must be allowed for that `vehicle_type` per `violation_policy` (two_wheeler / four_wheeler flags). Sets `priority = tier priority (3/2/1/0)`. Quota 10/day → 429, > 200 MB → 413. Status `uploading`. Returns `{video_id, storage_path, upload_url}`. |
| POST | /videos/upload/complete | owner | `{video_id}`; verifies object; status → `unprocessed` (= queued). |
| GET  | /videos?status=&limit=&offset= | any | citizen: own; officer/admin: all. Row includes `summary`, `priority`, `declared_violation`, `allegation_answer`, `user_status` (plain words: uploading, queued, analysing, awaiting_review, decided, withdrawn, could_not_process). |
| GET  | /videos/{id} | owner or reviewer | `{video, vehicle_records, cases: [{id, track_id, is_subject, status, identity_status, vehicle_type, plate, findings: [{id, violation, ai_result, tier, decision, rejection_reason, note_public}], evidence: [...]}], detection_video_url}`. **Citizens** (docs §0, §2.6 items 24-25): `evidence` is always `[]` and no evidence URL is ever signed — the uploader gets the textual outcome per finding plus the redacted `detection_video_url` (signed, only after the subject case is finalized); `vehicle_records` keep only track_id, vehicle_type, first_seen, last_seen, frames_observed; every plate (case `plate`, `plate_text`, `corrected_plate`) is `null` unless it is the subject's AND the uploader typed a `claimed_plate` AND `cases.identity_status = 'resolved'` AND the plate equals the claim once normalised — never a partial mask; `video` omits `blob_url`, `local_path`, `storage_path`, `error_reason` (`error_category` stays, `user_status` is the plain-word status). Reviewer identity and private notes never returned to citizens. |
| POST | /videos/{id}/withdraw | owner | optional body `{reason}`. Instant while status in (uploading, unprocessed) → `withdrawn`; while `processing`/`processed` with no case `in_review` → `withdrawn` + cases closed `withdrawn`; if any case is `in_review` → creates `withdrawal_requests` row (status pending, `reason` required, ≥ 5 chars, stored on the row) and notifies the claiming reviewer; 409 if already decided/finalized or a request is already pending. |
| POST | /videos/{id}/requeue | admin | audited. 409 unless status is `processed`/`failed` **and** no case of the video is `finalized` or `in_review` (a re-run creates a fresh set of cases; `persist_run_result` withdraws the previous run's still-open ones). |
| DELETE | /videos/{id} | admin | soft delete, audited. |

## 3. Cases and findings (reviewer, admin)
A case = one vehicle track in one run. Findings hang off it. AI values are immutable; corrections and
decisions are separate rows.

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | /cases?lane=normal\|not_supported&status=pending_review\|in_review\|second_opinion\|finalized&limit=&offset= | officer | ordered by `priority desc, created_at asc`. Each: `{id, video_id, run_id, track_id, is_subject, lane, status, priority, identity_status, vehicle_type, plate: {ai, confidence, claimed, corrected}, findings_count, claimed_by, claimed_at, allegation_answer}`. `lane=not_supported` is the manual-check lane for "AI: allegation not supported". |
| GET | /cases/mine | officer | cases claimed by me, with `idle_hours`. |
| POST | /cases/{id}/claim | officer | atomic RPC `claim_case(case_id)`: `pending_review\|second_opinion → in_review`, sets `claimed_by/claimed_at`; 409 if already claimed by someone else. A reviewer may hold at most 3 open claims (409). For `second_opinion`, the claimer must differ from the first reviewer. |
| POST | /cases/{id}/release | claimer or admin | back to previous status; admin release is audited `case.release`. |
| GET | /cases/{id} | claimer, or officer read-only, or admin | full: every column of the list row (same enrichment, so `vehicle_type`, `plate: {ai, confidence, claimed, corrected}`, `findings_count`, `allegation_answer`, `declared_violation` are present) plus `uploader_claim: {declared_violation, note, claimed_plate, vehicle_type}`, `allegation`, AI block (`ai: {track, plate_observations, resolved_plate, findings, pipeline_version, model_versions, vlm_calls}`), `evidence: {<finding_id>: [{...evidence row, url, expires_at}]}`, `corrections[]`, `decisions[]`, `plate_of_record` (string), `withdrawal_request` (the pending `withdrawal_requests` row for this case's video, or `null`), `plate_history: {confirmed: [...], observed: [...]}` ONLY when status is finalized (reviewers see history after deciding), admin always; `null` otherwise. |
| POST | /cases/{id}/findings/{fid}/decision | claimer | `{decision: confirmed\|rejected\|inconclusive, rejection_reason?: <rejection_reasons.code> (required when rejected), note?: string}`. 409 if case not `in_review` by caller. Writes `finding_decisions` row (append-only) and updates `findings.decision`. |
| POST | /cases/{id}/corrections | claimer | `{field: plate\|vehicle_type\|violation_label\|evidence, finding_id?: (for violation_label/evidence), ai_value, corrected_value, reason?: string}`. `reason` required for `plate`. `ai_value`/`corrected_value` are a string, or a list of strings for `field=evidence` (frame selection; stored as JSON text in the text column) — a list on any other field is 422. Append-only `case_corrections`; never edits AI columns. |
| POST | /cases/{id}/finalize | claimer | 422 unless every finding has a decision. `inconclusive` findings → case status `second_opinion` (unless it already had a second opinion → those findings become `unverifiable` and the case finalizes). Otherwise `finalized`, locked; writes `plate_history` confirmed rows for confirmed findings using the corrected plate if any; notifies uploader; if the plate reaches the escalation threshold creates an `escalations` row (`pending_approval`) and notifies admins. |
| POST | /admin/cases/{id}/reopen | admin | `{reason}` → status `reopened` → new review cycle (back to `pending_review` with `cycle+1`); original decisions kept; audited. |
| POST | /admin/cases/{id}/reassign | admin | `{reviewer_id}` releases and pre-assigns; audited. |
| GET | /rejection-reasons | officer | the pick-list. |

## 4. Plates, history, escalation
| Method | Path | Who | Notes |
|---|---|---|---|
| GET | /plates/{plate}/history | admin; officer only for a plate on one of their finalized cases | `{plate, status: clean\|watch\|escalated, confirmed: [...], observed: [...] (unconfirmed AI layer), escalations: [...]}`. Unconfirmed observations never move the status (docs §2.7 item 27): a plate with observations only is `clean`. |
| GET | /admin/escalations?status= | admin | |
| POST | /admin/escalations/{id}/approve | admin | builds the package (plate, confirmed cases, evidence signed for 7 days, decisions, reasons, reviewer badge numbers), stores it, status `approved`, plate status `escalated`, audited. |
| POST | /admin/escalations/{id}/dismiss | admin | `{reason}`, audited. |

## 5. Evidence access (private storage)
| Method | Path | Who | Notes |
|---|---|---|---|
| GET | /evidence/sign?path= | authorised per path | Returns a short-lived (15 min) URL for one blob. Officer/admin: any path. Citizen: only their own video's `detection_video_path`, and only once the subject case is finalized — every other path is 403, evidence frames included (docs §2.6 items 24-25). Backend signs Azure SAS with its own connection string. Blob paths are stored in the DB; never URLs. |

## 6. Admin
| Method | Path | Notes |
|---|---|---|
| GET | /admin/live | `{users_online (last_seen_at within 5 min), uploads_today, queue: {by_priority: {3,2,1,0}, depth}, processing: [{video_id, elapsed_s, attempts}], worker_last_seen, failed_today, review: {backlog, oldest_waiting_s, claimed, reviewers_active: [{id, name, open_claims}]}}` |
| GET | /admin/queue, POST /admin/queue/pause, POST /admin/queue/retry-failed | as v2 |
| GET | /admin/quality?days=30 | `{rejections_by_reason: {...}, agreement_by_violation: {violation: {confirmed, rejected, inconclusive}}, plate_misread_rate, not_supported_rate, avg_review_seconds}` computed from finding_decisions + case_corrections. |
| GET/PATCH | /admin/users, /admin/users/{id}/role | as v2; role change body `{role, reason, verified_via}` — `verified_via` required when promoting to officer (where the badge was checked). |
| GET | /admin/audit | as v2 |
| GET/PUT | /admin/settings | `max_uploads_per_day`, `escalation_threshold_any` (3), `escalation_threshold_top` (1), `retention_days`, `worker_paused`, `worker_lease_minutes` (120, 10–1440). |

## 7. Worker (no HTTP; direct Postgres as `drivetrust_ai_worker`)
- `select * from claim_next_video()` — now ordered `priority desc, uploaded_at asc`, same lease/reaper/pause rules.
- `select persist_run_result($1::jsonb)` — ONE call, ONE transaction, owned by the backend: validates
  `contract_version = '2.0'`, upserts `vehicle_records`, inserts `plate_observations`, `findings`
  (deterministic `finding_id`), `evidence`, creates one `cases` row per vehicle track that has any
  tier A/B finding (subject track always gets a case, lane `not_supported` when the allegation answer is
  `not_supported`/`unobservable`; `ambiguous_subject` and `manual_review` stay in lane `normal`; accepted
  answers = contract `ALLEGATION_ANSWERS`), sets `videos.status = 'processed'`, `summary`, `allegation_answer`,
  `run_id`, `detection_video_path`. Idempotent: same `run_id` twice → no-op. Raises on any invariant
  violation (nothing partial persists). `run_id = sha1(video_id:attempts)`, so requeue / retry-failed never
  reset `attempts`: every re-run gets a fresh `run_id`.
- `select public.renew_lease($1::uuid)` — **required heartbeat while a clip is in flight**: sets `claimed_at = now()`
  on a `processing` video. `claim_next_video()` reaps leases older than `system_settings.worker_lease_minutes`
  (default 120) and re-queues them, so a long run that never renews is reaped mid-processing. Returns false when
  the video is no longer `processing` (withdrawn / requeued / already reaped) — abandon the run.
- a re-run supersedes the previous one: `persist_run_result` withdraws that video's still-open cases from older
  `run_id`s, so a requeue never duplicates cases or double-counts escalation.
- failure → `videos.status='failed'`, `error_reason`, `error_category` (download | pipeline | contract | persist).
- heartbeat as v2.

## 8. Notifications (DB triggers, unchanged mechanism)
| Event | Recipients |
|---|---|
| submission received / analysed / decided / withdrawal answered | uploader |
| new top-tier case; own claim idle 24 h; second-opinion request | reviewers |
| top-tier submission; processing failure; worker silent 10 min; reviewer request pending; escalation threshold; withdrawal request on in_review case | admins |

## 9. Data model additions (SQL migration `003_cases_findings_priority.sql`)
- `videos`: `declared_violation text`, `claimed_plate text`, `note text`, `recording_at timestamptz`, `location_text text`, `consent_at timestamptz`, `priority int default 0`, `allegation_answer text`, `error_category text`, `detection_video_path text`, `withdrawn_at timestamptz`.
- `profiles`: `last_seen_at timestamptz`, `verified_via text`, `deactivated_at timestamptz`.
- `plate_observations(id, video_id, run_id, track_id, text, engine, confidence, frame_index, timestamp, crop_path)`.
- `cases(id uuid, video_id, run_id, track_id, vehicle_record_id, is_subject bool, lane text, status text, cycle int default 1, priority int, identity_status text, claimed_by uuid, claimed_at, finalized_at, finalized_by, reopened_by, reopen_reason, created_at)` unique `(run_id, track_id)`.
- `findings(id text = finding_id, case_id, video_id, track_id, violation, ai_result, tier, confidence, agreement, evidence_frames, evaluable_frames, reasoning, vlm_call_id, decision text default 'pending', decided_by, decided_at, rejection_reason, note, created_at)`.
- `finding_decisions(id, finding_id, case_id, cycle, reviewer_id, decision, rejection_reason, note, created_at)` append-only.
- `case_corrections(id, case_id, finding_id, field, ai_value, corrected_value, reason, reviewer_id, created_at)` append-only.
- `evidence(id, finding_id, case_id, track_id, frame_index, timestamp, blob_path, sha256, created_at)`.
- `rejection_reasons(code pk, label, sort)` seeded: helmet_worn, wrong_vehicle, plate_misread, footage_unclear, not_a_violation, duplicate_case, other.
- `plate_history(id, plate, layer 'observed'|'confirmed', finding_id, case_id, video_id, violation, created_at)`; view `plate_status(plate, status, confirmed_count, top_tier_confirmed, observed_count)`.
- `escalations(id, plate, status pending_approval|approved|dismissed, threshold_hit text, package jsonb, approved_by, dismissed_reason, created_at)`.
- `withdrawal_requests(id, video_id, requested_by, status pending|accepted|declined, decided_by, reason, created_at)`.
- `violation_policy`: add `tier text`, `two_wheeler bool`, `four_wheeler bool`, `review_only bool`; re-seed from `pipeline/contract.py` VIOLATION_POLICY.
- RPCs: `claim_case(uuid)`, `release_case(uuid)`, `finalize_case(uuid)`, `persist_run_result(jsonb)`, `claim_next_video()` (priority order), `renew_lease(uuid)` (worker heartbeat).
- `system_settings`: `worker_lease_minutes` (default 120, admin-editable via `/admin/settings`) — the stale-lease window used by `claim_next_video()`.
- `plate_history` observed layer is written only for findings whose AI result is `confirmed`/`needs_review` **and whose case `identity_status = 'resolved'`**; `finalize_case` writes the confirmed layer from the reviewer's correction, else the AI plate **only when `cases.identity_status = 'resolved'`**. Neither layer ever holds an unresolved read (docs §2.7 item 27). The `plate_status` view ignores `observed_count`: observations are model-QA data, never a signal shown during review.
- RLS: citizens read own videos and own cases/findings rows (plates masked in the API, not RLS); `evidence` is **reviewer-only** in RLS too, since citizens never receive frames; officers read all cases/findings/evidence, write only via RPC/backend; admins all; worker: execute the two RPCs + select videos + update system_settings. `plate_history`, `escalations`, `finding_decisions`, `case_corrections`: insert-only for humans via backend, no update/delete grants.
- Realtime publication: add `cases`, `findings`.

## 10. Status vocab
- videos.status: `uploading | unprocessed | processing | processed | failed | withdrawn` (user words mapped in the API).
- cases.status: `pending_review | in_review | second_opinion | finalized | reopened | withdrawn`.
- findings.decision: `pending | confirmed | rejected | inconclusive | unverifiable`.
- plate status (view): `clean | watch | escalated` (observations never move it, docs §2.7 item 27).
