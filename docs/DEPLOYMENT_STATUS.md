# RoadWatch.AI — Deployment status (1 October 2026)

Status of every component, what changed in this release, and the short list of things
only the project owner can do (dashboard clicks). Share this with the team as the update.

## 1. Where things stand

| Component | Where | State today | Owner action |
|---|---|---|---|
| **Frontend** (static web app) | Vercel · https://raksharider.vercel.app | Live. New landing page, dashboards and footer ship with this commit; Vercel auto-deploys on push. | Check the deploy finished in the Vercel dashboard. |
| **Backend** (FastAPI) | Render · https://raksharider.onrender.com | **Suspended by owner** (Render shows "This service has been suspended by its owner"). Code + blueprint are pushed and tested (53 tests). | **Resume** the service in the Render dashboard; confirm the 4 env vars (section 3). |
| **Database + auth** | Supabase · project `fbjjoktuzirhpqqpzfbo` | **Live** (REST 200, GoTrue healthy, email + Google sign-in enabled). Migrations 002, 003, 004 (partly), 006 are applied. | Run `backend/database/007_decide_finding_merge.sql` in the SQL editor (section 3). |
| **AI worker** (YOLO + OCR queue worker) | Laptop (`new ai pipeline/start.bat`) | Verified end to end today on the RTX 3060. Docker image builds (5.7 GB, CPU) and boots with a health endpoint. | Keep running it on the laptop, or host it free on a Hugging Face Space (`new ai pipeline/DEPLOY.md`). |
| **Keep-alive** (never sleeps) | Inside the backend + GitHub Actions | Ships with this commit (section 2). | Optional: add 2–3 GitHub secrets for the external pinger. |

## 2. What changed in this release

**Backend (3.1.0)**
- `GET /status` — public, cached (60 s) platform numbers: worker heartbeat, queue depth, intake switch, clips analysed, cases decided, findings confirmed. Aggregates only, no rows.
- `GET /health?deep=1` — probes Supabase (cached 60 s) so every health ping is database activity.
- Keep-alive thread (`app/services/keepalive.py`): every 10 min it pings its own public URL (Render never idles) and runs `check_idle_alerts()` (24 h idle alert, 48 h auto-release, "worker silent" alert). That routine was never scheduled before because pg_cron is off on the project.
- `CORS_EXTRA_ORIGINS` env var for a custom domain; Render blueprint (`render.yaml`) updated with the new vars.
- Tests: 53 pass (`backend/app/tests`).

**Database**
- `007_decide_finding_merge.sql`: migration 006 had silently dropped the rule from 004 that a finding cannot be confirmed with zero evidence frames (checked on the live function). 007 restores it, keeps 006's plate-history cleanup, re-applies the profile column grants, and grants the backend EXECUTE on `check_idle_alerts`.

**Frontend (web v5.0)**
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
3. **Supabase → Authentication → URL configuration:** Site URL `https://raksharider.vercel.app`, and `https://raksharider.vercel.app/**` in Redirect URLs (needed for Google sign-in to land back on the app).
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
| Backend tests | 53 passed |
| Pipeline unit tests | 136 passed |
| Frontend tests | pass (`node --test frontend/tests/`) |
| Pipeline on `tests/sample_videos/sample-3.mp4` (4K, 20 s, 240 frames at 15 fps, RTX 3060) | 502 s; package valid (contract 2.0); 46 vehicles, 92 findings (2 queued), 27 evidence frames, detection video 12.8 s; allegation "no_helmet / two_wheeler" answered **not_supported** (the clip has no two-wheeler, which is the correct answer) |
| Docker image `roadwatch-worker` | builds, 5.74 GB, boots in ~6 s, `/health` 200 |
| Supabase | REST 200, GoTrue v2.197, email + Google providers on |
| Render | 503 "suspended by its owner" until resumed |

## 6. Notes and limits

- **Free-tier cold start.** Even with keep-alive, a redeploy or a crash means the first request takes 30–60 s. The frontend now waits and retries instead of showing an error.
- **Supabase pause.** Free projects pause after 7 days without API activity. The backend's deep health probe runs every 10 minutes, so as long as Render is up the project never goes idle; the GitHub workflow is the second line of defence.
- **Compute.** A 20 s 4K clip takes ~8 min on the RTX 3060 at dense 15 fps / 1280 px. On a CPU host (Hugging Face Space) expect several times longer; the queue lease (2 h, renewed every 60 s) covers it.
- **Roles.** `citizen | officer (Reviewer) | admin` live only in `public.profiles.role`; the backend checks the role on every request; sign-ups are always citizens and reviewer access is approved by an admin after a badge check (Users page).
- Vercel and Render deploy from the GitHub repo on push (`main`); this release was pushed to `clean-main` and merged into `main`.
