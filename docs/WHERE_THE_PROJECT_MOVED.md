# Where the project moved (October 2026)

RakshaRide / RoadWatch.AI continues in two repositories:

| Repository | What lives there |
|---|---|
| `roadwatch-backend` | the FastAPI API, the AI queue worker (YOLO + BoT-SORT + PaddleOCR), the Supabase SQL migrations (001–013) and the deployment handover for the OVH server |
| `roadwatch-frontend` | the static web app: landing page, citizen uploads, reviewer queue, admin console; nginx image with runtime config |

This repository keeps the earlier code and the project documents (`docs/`). The Render backend
(`render.yaml`) and the Vercel frontend (`frontend/vercel.json`) were deployed from here; the
OVH deployment of 7 Oct 2026 runs from the two repositories above.

What changed after the move, in short:

- plates are read by PaddleOCR with a best-guess identity rule (pipeline 3.1);
- every analysis is a permanent run record with the exact model files by SHA-256, plus a model
  registry, dataset versions and a locked evaluation set (pipeline 3.2, SQL 011);
- the detection video draws only the vehicles of record, as shapes with the words outside the
  box; a two-wheeler report tracks two-wheelers only; reviewers see the live stage and frame
  counter while a clip is analysed; the queue is grouped by upload (pipeline 3.3, SQL 013).
