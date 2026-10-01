# RoadWatch.AI — Deployment status (1 October 2026)

Status of every component, what changed in this release, and the short list of things
only the project owner can do (dashboard clicks). Share this with the team as the update.

## 1. Where things stand

| Component | Where | State today | Owner action |
|---|---|---|---|
| **Frontend** (static web app) | Vercel · https://raksharider.vercel.app | Live. New landing page, dashboards and footer ship with this commit; Vercel auto-deploys on push. | Check the deploy finished in the Vercel dashboard. |
| **Backend** (FastAPI) | Render · https://raksharider.onrender.com | **Live** (resumed 1 Oct evening, 3.1.0, keep-alive running, 74 tests). | Confirm the 4 env vars (section 3). |
| **Database + auth** | Supabase · project `fbjjoktuzirhpqqpzfbo` | **Live** (REST 200, GoTrue healthy, email + Google sign-in enabled). Migrations 002, 003, 004 (partly), 006 are applied. **The live schema is behind the code**: `videos_status_check` still has the pre-002 values, so uploads fail at init and AI results cannot be saved (section 3, step 2c). | Run `007`, `008` and `009` in the SQL editor, in that order (section 3). |
| **AI worker** (YOLO + OCR queue worker) | Laptop (`new ai pipeline/start.bat`) | Verified end to end today on the RTX 3060. Docker image builds (5.7 GB, CPU) and boots with a health endpoint. | Keep running it on the laptop, or host it free on a Hugging Face Space (`new ai pipeline/DEPLOY.md`). |
| **Keep-alive** (never sleeps) | Inside the backend + GitHub Actions | Ships with this commit (section 2). | Optional: add 2–3 GitHub secrets for the external pinger. |

## 2. What changed in this release

**Backend (3.1.0)**
- `GET /status` — public, cached (60 s) platform numbers: worker heartbeat, queue depth, intake switch, clips analysed, cases decided, findings confirmed. Aggregates only, no rows.
- `GET /health?deep=1` — probes Supabase (cached 60 s) so every health ping is database activity.
- Keep-alive thread (`app/services/keepalive.py`): every 10 min it pings its own public URL (Render never idles) and runs `check_idle_alerts()` (24 h idle alert, 48 h auto-release, "worker silent" alert). That routine was never scheduled before because pg_cron is off on the project.
- The deep probe is bounded (6 s per request, 8 s budget checked between tables, one probe at a time with a stale result for callers that find one in flight, stops after the first connection failure, 'degraded' when it runs out of budget) and distinguishes a missing GRANT (SQLSTATE 42501, read back with a GET when the HEAD gives a bodiless 403) from a gateway 403 or a rejected service key.
- Upload errors never forward PostgREST's error dict (its details carry the failing row with storage paths); a CHECK violation names the constraint and 009, a missing grant names 008, anything else gives only the SQLSTATE. The declared-violation lookup is guarded the same way (it is refused with 42501 today, until 008 runs).
- A missing `profiles` row is created on the user's first authenticated request (always `citizen`); `PUT /auth/profile` answers 404 instead of echoing the request when no row matched.
- An upload that trips a database CHECK constraint now says "the database schema is out of date (videos_status_check) — run 009" instead of returning the raw PostgREST error with the failing row.
- `CORS_EXTRA_ORIGINS` env var for a custom domain; Render blueprint (`render.yaml`) updated with the new vars.
- Tests: 53 pass (`backend/app/tests`).

**Database**
- `007_decide_finding_merge.sql`: migration 006 had silently dropped the rule from 004 that a finding cannot be confirmed with zero evidence frames (checked on the live function). 007 restores it, keeps 006's plate-history cleanup, re-applies the profile column grants, and grants the backend EXECUTE on `check_idle_alerts`.
- `008_service_role_grants.sql`: SELECT/INSERT/UPDATE for the backend's service role on the 002-era tables (`system_settings`, `audit_log`, `vehicle_records`, `violation_policy`), which the live catalog shows were never granted.
- `009_live_schema_catchup.sql` (**found 1 Oct night, blocks every upload**): the live `videos_status_check` constraint still allows only `unprocessed | processing | completed | failed`. The backend writes `uploading` at upload init and `withdrawn` on withdrawal, and `persist_run_result` has written `processed` since 003 — all refused with SQLSTATE 23514. Result today: "Failed to init video upload … violates check constraint" on Submit, and the 8 clips the worker did analyse could never be saved. Two more pre-002 leftovers block the same save: `vehicle_records_review_status_check` refuses the `needs_review` / `clear` values the function writes, and `evidence` still has NOT NULL `vehicle_id` / `image_url` columns the function never fills plus no unique `(finding_id, blob_path)` for its ON CONFLICT. 009 widens both constraints, relaxes the two columns, creates the index, backfills `profiles` rows for users created before the sign-up trigger existed (cause of the browser's 406 on its own profile), and optionally releases the 8 legacy clips stuck in `processing` without a lease (the 4 with a stored file go back in the queue; the 4 without one are marked failed).

**Frontend (web v5.0, updated 1 Oct night)**
- Sign-in errors are now explained by GoTrue error *code* in a persistent notice under the form (a toast vanished in 3 s): unconfirmed e-mail (with a "Resend confirmation e-mail" link; a failed login never sends mail by itself, because the built-in mailer allows two e-mails per hour for the whole project), wrong password (with the hint that a Google-created account has no password), rate limits, banned account, built-in-mailer refusal (points at the owner's SMTP setting), network failure (points at `/reset`). Three wrong passwords in a row pause the button for 5 s. Enter submits. An existing address that re-registers is told so instead of "Account created". Failed e-mail/OAuth links (`#error_code=otp_expired…`) are shown instead of ignored.
- Every e-mail and OAuth link now returns to the site root (never `/reset`, never `index.html#admin`), and opening `/reset` sends you straight back to the home page after clearing the old caches.
- The password-reset link now reliably opens the "Choose a new password" dialog: it used to be opened on a timer before the app shell was on screen and was destroyed by the router, so on a normal connection it never appeared.
- New landing page: clean top bar with section links, editorial hero with an illustration panel, **live stats bar** (from `/status`), "How it works", sample analysis, roles, privacy, call-to-action band and a **real footer** with product / account / trust / project links and a live API + worker indicator.
- Dashboards: a one-line **system status strip** (API, AI worker, queue) on the citizen home and the reviewer queue; a **"Where your clips are"** pipeline panel on the citizen home; an in-app footer with version and status.
- Cold-start handling for the Render free tier: GET requests retry with backoff for up to ~35 s and show one "waking up the server" toast instead of failing.
- Login and sign-up screens unchanged. Not a PWA (manifest removed; the old service worker only uninstalls itself).

**AI worker**
- `worker.py` gains an optional HTTP health endpoint (`WORKER_HTTP_PORT`, 7860 in Docker) with last heartbeat, current job and counters. The pipeline itself (`pipeline/`, `run_pipeline.py`, contract 2.0) is untouched.
- `Dockerfile` hardened: CPU torch wheels, non-root user, writable cache dirs, `HEALTHCHECK`, `EXPOSE 7860`. `DEPLOY.md` documents laptop, Hugging Face Space and generic Docker hosting.

**Keep-alive from outside**
- `.github/workflows/keepalive.yml` runs every 10 minutes: backend `/health?deep=1` (4 retries for cold starts), optional direct Supabase REST ping, optional worker ping.

## 3. Owner checklist (about 10 minutes)

1. **Render → roadwatch-backend → Resume.** Then Environment tab: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_EVIDENCE_CONTAINER=evidence` must be present (`KEEPALIVE_ENABLED=1` comes from the blueprint). The blueprint points at the repo root `render.yaml`, Python 3.11.9.
2. **Supabase → SQL editor → paste and run `backend/database/007_decide_finding_merge.sql`.** Verify with the two-line query at the bottom of the file (expect `true | true`).
2b. **Supabase → SQL editor → run `backend/database/008_service_role_grants.sql`.** Found after the Render resume on 1 Oct: the tables created by migration 002 (`system_settings`, `audit_log`, `vehicle_records`, `violation_policy`) carry no SELECT/INSERT/UPDATE for `service_role`, so the backend's settings reads, audit writes, case/plate enrichment and uploads with a declared violation are refused. `GET /health?deep=1` lists the refusing tables under `database.denied` until this is run.
2c. **Supabase → SQL editor → run `backend/database/009_live_schema_catchup.sql`.** Until this runs, **Submit clip fails for everyone** and the worker cannot save a finished analysis (see "Database" above). The SELECTs at the end verify it: two constraint definitions, the evidence index count 1, and three zero counts.
3. **Supabase → Authentication → URL configuration:** Site URL `https://raksharider.vercel.app`, and `https://raksharider.vercel.app/**` in Redirect URLs (needed for Google sign-in, confirmation and password-reset links to land back on the app). Google Cloud Console: the OAuth client's authorised redirect URI is `https://fbjjoktuzirhpqqpzfbo.supabase.co/auth/v1/callback`.
3b. **Supabase → Authentication → SMTP Settings:** the project is on the built-in mailer, which delivers only to organisation members and at most 2 e-mails per hour. Anyone else who signs up never gets the confirmation link and then sees "e-mail not confirmed" forever. Configure a real SMTP sender (Resend, Brevo, Gmail app password…) before public testing. Also check **Authentication → Users** for the affected tester: `email_confirmed_at`, `banned_until`, and whether the identity is Google-only (a Google-created account has no password).
4. **GitHub → Settings → Secrets and variables → Actions** (optional but recommended): `SUPABASE_URL`, `SUPABASE_ANON_KEY` (the publishable key already used by the frontend). Add `WORKER_HEALTH_URL` if the worker is hosted. Check the `keepalive` workflow runs green under the Actions tab. GitHub pauses scheduled workflows after 60 days without a push; any commit re-enables it.
5. **AI worker:** keep `start.bat` running on the laptop, or follow `new ai pipeline/DEPLOY.md` section B for a free always-on Space.

## 4. How to verify after the Resume

| Check | Expect |
|---|---|
| `https://raksharider.onrender.com/health?deep=1` | `status: healthy`, `database.ok: true`, `keepalive.running: true` |
| `https://raksharider.onrender.com/status` | `ok: true`, `worker.online: true` while the laptop worker runs |
| https://raksharider.vercel.app | Landing page with the stats bar showing numbers (not "—") within a minute |
| Sign in as admin → Live board | *Worker heartbeat* **OK**; users online, queue depth |
| Sign in as a citizen → Upload a clip | Queued → Analysing → Awaiting review (the admin Processing page shows it) |
| Reviewer → Queue → Claim → decide → Finalize | Confirm is refused with "no evidence frames" on a finding without evidence (proves 007 is live) |

## 5. Measured today

| What | Result |
|---|---|
| Backend tests | 74 passed |
| Pipeline unit tests | 136 passed |
| Frontend tests | pass (`node --test frontend/tests/`) |
| Pipeline on `tests/sample_videos/sample-3.mp4` (4K, 20 s, 240 frames at 15 fps, RTX 3060) | 502 s; package valid (contract 2.0); 46 vehicles, 92 findings (2 queued), 27 evidence frames, detection video 12.8 s; allegation "no_helmet / two_wheeler" answered **not_supported** (the clip has no two-wheeler, which is the correct answer) |
| Docker image `roadwatch-worker` | builds, 5.74 GB, boots in ~6 s, `/health` 200 |
| Supabase | REST 200, GoTrue v2.197, email + Google providers on |
| Render | 503 "suspended by its owner" until resumed |

## 5b. If the app looks old or sign-in fails with "Failed to fetch"

Browsers that installed the retired PWA service worker (builds up to `app.js?v=2.2`; the console
shows `sw.js:33 Failed to fetch` on the login request) can keep serving a stale page. Three layers
fix that: the new page unregisters any worker and clears its caches on load; `/sw.js` is a
self-destructing worker; and opening **https://raksharider.vercel.app/reset** once sends a
`Clear-Site-Data` header that wipes the old caches and storage, then returns you to the home page
(sign in again afterwards). If the error persists after `/reset`, a browser extension or the
network is blocking requests from the site (the console's `chrome-extension` line is the tell):
retry with extensions disabled, or DevTools → Application → Service Workers → Unregister.
Note that any link to `/reset`, from anywhere, signs the visitor out of this site in that browser
(nothing else: no other origin, no cookies, and the refresh token is not revoked). That is the
point of the page; remove the `/reset` header rule from `frontend/vercel.json` once the old
service-worker population has drained.

## 6. Notes and limits

- **Free-tier cold start.** Even with keep-alive, a redeploy or a crash means the first request takes 30–60 s. The frontend now waits and retries instead of showing an error.
- **Supabase pause.** Free projects pause after 7 days without API activity. The backend's deep health probe runs every 10 minutes, so as long as Render is up the project never goes idle; the GitHub workflow is the second line of defence.
- **Compute.** A 20 s 4K clip takes ~8 min on the RTX 3060 at dense 15 fps / 1280 px. On a CPU host (Hugging Face Space) expect several times longer; the queue lease (2 h, renewed every 60 s) covers it.
- **Roles.** `citizen | officer (Reviewer) | admin` live only in `public.profiles.role`; the backend checks the role on every request; sign-ups are always citizens and reviewer access is approved by an admin after a badge check (Users page).
- Vercel and Render deploy from the GitHub repo on push (`main`); this release was pushed to `clean-main` and merged into `main`.
- **`frontend/vercel.json` header order matters.** On Vercel the *later* matching `headers` rule wins. The catch-all `/(.*)` (1 h cache) must stay first and the specific `/`, `/index.html`, `/app.js`, `/sw.js`, `/manifest.json`, `/reset` rules after it. The original file had them the other way round, which is why `/sw.js` and the HTML were cached for an hour from 13 Sep to 1 Oct and old clients never saw the cleanup worker.
