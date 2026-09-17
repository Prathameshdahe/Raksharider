# RoadWatch.AI (RakshaRide)

> AI-assisted traffic-violation detection from dashcam clips. The pipeline finds vehicles,
> attaches every finding to **one** tracked vehicle with its own evidence, and a human
> reviewer decides before anything counts. Nothing issues a penalty automatically.

## Layout

```
RakshaRide/
├── frontend/          static web app (Vercel) — role-aware dashboard, NOT a PWA
├── backend/           FastAPI (Render) — auth, uploads, review, admin; role checks live here
│   ├── API.md         endpoint contract (frontend ↔ backend)
│   └── database/      SQL to run in Supabase (see "Database setup")
├── new ai pipeline/   detection pipeline + queue worker (runs on a laptop with the models)
│   ├── pipeline/contract.py   the ONE schema shared by pipeline and worker
│   └── tests/                 115 unit tests, no GPU or network needed
└── start.bat          local launcher (backend :8000, frontend :5051, worker)
```

## How a clip flows

```
citizen uploads ──► backend: quota + size check, signed upload URL, row status 'uploading'
                    client PUTs file to Supabase Storage, backend verifies the object → 'unprocessed'
worker (laptop) ──► claim_next_video()  fair round-robin across uploaders, 2h lease, zombie reaper, pause switch
                    run_pipeline.run()  per-vehicle findings → verdicts → per-track evidence JPEGs
                    ONE transaction: vehicle_records + violations + videos.status='processed' (or 'failed' + reason)
DB triggers     ──► notifications to the uploader; reviewers get "new clip awaiting review"
reviewer        ──► confirms / rejects with a reason (audit row + score ledger written on confirm)
```

## Roles

| Role (DB value) | UI label | Can |
|---|---|---|
| `citizen` | Citizen | upload (10/day, 200 MB), see own clips with masked plates, get alerts |
| `officer` | Reviewer | everything above + review queue, confirm/reject with reason, correct plate/type |
| `admin` | Admin | everything + queue health, pause/resume intake, requeue/retry, user roles, audit log |

Signup **always** creates a citizen; "I am a traffic officer" only records a request an admin
must approve. Role is read from `public.profiles.role` (server-side) — never from JWT metadata.
The frontend hiding a button is cosmetic; `require_role()` in the backend is the boundary.

## Quick start (local)

```bat
.\start.bat
```

Manual:

```bat
cd backend && venv\Scripts\activate && uvicorn app.main:app --port 8000 --reload
cd frontend && python -m http.server 5051
cd "new ai pipeline" && python worker.py --poll 10        # add --vlm to allow external model calls
```

Run one clip without the queue:

```bat
cd "new ai pipeline"
python run_pipeline.py tests\sample_videos\sample-3.mp4 --track
```

Output: `pipeline/evidence_output/latest/report.json`, `track_log.json`, `tracks/<track_id>/*.jpg`.

## Environment

**backend/.env**
```
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=        # backend only — never in the worker, never in git
```

**new ai pipeline/.env** (see `.env.example`)
```
DB_HOST= DB_PORT= DB_NAME= DB_USER=drivetrust_ai_worker.<ref> DB_PASSWORD=
AZURE_STORAGE_CONNECTION_STRING=  AZURE_EVIDENCE_CONTAINER=evidence
GEMINI_API_KEY= / NVIDIA_NIM_API_KEY=      # only used with --vlm
PIPELINE_API_KEY=                          # only if you run api/main.py
```

The worker connects as the scoped Postgres role and refuses to start without the DB vars.
It has **no** service key: a service key bypasses row-level security and would let a worker
bug touch trust scores, roles or human decisions.

> **Action required:** a Supabase service-role key was committed in the past (commit `a642846`,
> old `worker.py`). Rotate it in the Supabase dashboard; the new code never reads it.

## Database setup (Supabase SQL editor, in this order)

1. `backend/database/auth_admin_setup.sql` — profiles, `is_admin()`
2. `backend/database/002_rbac_queue_notifications.sql` — everything else (idempotent):
   role guard trigger, queue columns, `vehicle_records`/`violations` shape, `violation_policy`,
   `notifications`, `audit_log`, `score_ledger`, `system_settings`, fair `claim_next_video()`,
   RLS for every table, worker grants, realtime publication.
3. Create the first admin with `backend/app/scripts/create_admin_user.py` (no hardcoded admin e-mail anymore).
4. Verify: as `drivetrust_ai_worker`, `insert into score_ledger …` must fail with permission denied.

Status vocab: `videos.status` = uploading → unprocessed → processing → processed | failed.
`vehicle_records.review_status` = clear | needs_review | confirmed | rejected.

## Pipeline v2.1 — what changed and why

| Bug (from the forensic review) | Fix |
|---|---|
| frame-wide violations copied to every vehicle | `rules.py` emits one `VehicleFinding` per vehicle; `run_pipeline.py` attributes it to exactly one track by IoU |
| "no helmet detection" treated as "no helmet" | `no_helmet` needs a positive `no_helmet` box on the head; otherwise *unobservable* |
| duplicate observations counted as evidence | one observation per (track, violation, frame); agreement over evaluable frames only |
| `first_seen` was a pixel coordinate | timestamps come from frame timestamps; `contract.py` asserts `0 ≤ first ≤ last ≤ duration` |
| pipeline/worker field names differed | `pipeline/contract.py` `VehicleRecord` is produced and parsed by both; a rename fails at parse time |
| job "processed" even when saving failed | worker writes records, violations and status in one transaction; any failure → `failed` + reason |
| evidence overwrote across videos / shared by all vehicles | `{video_id}/{run_id}/{track_id}/{frame}.jpg`, attached per record |
| specialist vehicle model wiped generic detections | spatial fusion: replace only the overlapping box |
| BGR→RGB swap before Ultralytics | removed (A/B: plate model 8 vs 5 detections on BGR; COCO neutral) |
| red light + rider = violation | `signal_violation` disabled by policy until stop-line geometry exists |
| wheelie from aspect ratio | review-only: can never be "confirmed" by geometry |
| global best plate stole plates across vehicles | plates resolved strictly per track; must pass Indian format **and** a real state code |
| severity mixed OCR quality into seriousness | `severity` = policy per violation type; `evidence_strength` = the old composite |
| three components disagreed | clip verdict is derived from per-vehicle verdicts; VLM writes back into the vehicle state and every call is logged |
| car merger merged tracks with empty plates | empty reads never merge; timestamps are real so time windows work |

Verdict vocabulary per vehicle and violation: `confirmed | needs_review | observed_absent | unobservable | not_evaluated`.

## Tests

```bat
cd "new ai pipeline" && python -m pytest tests -q          # 115 tests, ~2 s
cd backend && venv\Scripts\python -m pytest app\tests -q   # 23 tests
```

`tests/test_attribution.py` is the golden test: two motorcycles, one helmetless rider → exactly one flagged track.

## Deployment

- **Frontend** → Vercel (static). Set `RENDER_URL` in `frontend/app.js` if the backend URL changes.
- **Backend** → Render, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`; env vars above.
  Replace the `"*"` CORS entry in `app/main.py` with the exact Vercel origin before going public.
- **Worker** → laptop with GPU, or `docker compose up` in `new ai pipeline/` (CPU image).

## Known gaps / next

- Retraining is on hold until attribution has been validated on a hand-labelled clip; training on the old
  misattributed labels teaches the wrong thing.
- Notifications are one-per-event; a daily digest (one message for "your 8 clips: 2 flagged") is a follow-up.
- Citizens see masked plates on their own uploads (server-side). A "my vehicle was flagged" view needs a
  plate-claim/verification step that does not exist yet.
- `missing_plate` is never raised per vehicle: the plate detector's recall is too low for absence to mean anything.
- `api/main.py` (legacy `/analyze`) now needs `PIPELINE_API_KEY`; it still runs the older clip-level path.
