# DriveTrust-Backend
## Roles, cases, plates and admin (v3)

Full contract: [`API.md`](API.md) (implements `docs/PRODUCT_FLOW_DECISIONS.md`). Role is resolved
from `public.profiles.role` (never JWT metadata) by `require_role(...)` in `app/utils/auth.py`.

**Setup**
1. Run, in order, in the Supabase SQL editor (all idempotent):
   `database/auth_admin_setup.sql` → `database/002_rbac_queue_notifications.sql` →
   `database/003_cases_findings_priority.sql`. Create the `drivetrust_ai_worker` Postgres role
   first if you want the worker grants applied. 003 needs `pgcrypto` (present on Supabase); the
   "worker silent" / "claim idle" alerts are scheduled automatically only if `pg_cron` is enabled,
   otherwise call `select public.check_idle_alerts()` from any scheduler every 5 min.
2. Bootstrap the first admin with `app/scripts/create_admin_user.py`. Public signup
   always creates a `citizen`; `role: "officer"` in the payload only sets `requested_role`
   (admins get a "reviewer request pending" notification).
3. Env (`.env`): `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, plus
   **`AZURE_STORAGE_CONNECTION_STRING`** (and optional `AZURE_EVIDENCE_CONTAINER`, default
   `evidence`) so the backend can sign 15-minute read URLs for evidence frames and detection
   videos. Without it `/evidence/sign` returns 503 and case/video detail returns evidence rows
   with `url: null`.
4. Daily upload quota, escalation thresholds and retention live in `system_settings`
   (`GET/PUT /admin/settings`); max file size is 200 MB (`MAX_VIDEO_BYTES`).

**Endpoints**
- Auth: `POST /auth/signup`, `POST /auth/login`, `GET /auth/me` (returns `portal` + `nav`), `PUT /auth/profile`, `POST /auth/presence`
- Submissions: `POST /videos/upload/init` (claim fields: `vehicle_type`, `declared_violation`, `claimed_plate`, `note`, `recording_at`, `location_text`, `consent`; sets `priority`), `POST /videos/upload/complete`, `GET /videos` (`user_status` in plain words), `GET /videos/{id}` (cases + findings + signed evidence; citizens see masked non-subject plates, no reviewer identity, detection video after the subject case is finalized), `POST /videos/{id}/withdraw` (instant / request / 409), `POST /videos/{id}/requeue` (admin), `DELETE /videos/{id}`
- Cases (officer/admin): `GET /cases?lane=&status=`, `GET /cases/mine`, `GET /cases/{id}`, `POST /cases/{id}/claim|release|finalize`, `POST /cases/{id}/findings/{fid}/decision`, `POST /cases/{id}/corrections`, `POST /cases/{id}/withdrawal` (answer the uploader's request), `GET /rejection-reasons`
- Plates: `GET /plates/{plate}/history` (admin; officer only after finalizing a case on that plate)
- Evidence: `GET /evidence/sign?path=` (Azure SAS, 15 min, per-path authorisation)
- Admin: `GET /admin/live`, `GET /admin/quality?days=`, `GET /admin/queue`, `POST /admin/queue/pause|retry-failed`, `GET /admin/users`, `PATCH /admin/users/{id}/role` (`verified_via` required when promoting to officer), `GET /admin/audit`, `GET/PUT /admin/settings`, `GET /admin/escalations`, `POST /admin/escalations/{id}/approve|dismiss`, `POST /admin/cases/{id}/reopen|reassign`
- Legacy v2 review routes (`/review/*`) still exist for the old `vehicle_records.review_status` flow.

**Worker** talks to Postgres directly as `drivetrust_ai_worker` (no service key):
`select * from claim_next_video()` (priority desc, uploaded_at asc, leased) and
`select persist_run_result($1::jsonb)` — one call, one transaction, contract 2.0, idempotent on `run_id`.

Tests: `venv\Scripts\python.exe -m pytest app/tests -q`
