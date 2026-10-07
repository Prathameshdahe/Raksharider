# RoadWatch changelog, October 2026

The work below shipped in `roadwatch-backend` and `roadwatch-frontend`; see
`WHERE_THE_PROJECT_MOVED.md`.

## Pipeline 3.1, plates (7 Oct)

- PaddleOCR (PP-OCRv6) replaces EasyOCR as the plate reader; EasyOCR stays as the fallback.
- Up to 12 candidate crops per vehicle, the best 6 read; a near-duplicate guard keeps two agreeing
  misreads from becoming a wrong plate of record.
- Unresolved plates reach the reviewer as a best guess with candidates and the stored crops.
- Measured on the Mumbai taxi clip: 8 of 9 labelled plates found (was 1 of 9), 6 right and 0 wrong.

## Pipeline 3.2, data layer (7 Oct)

- One `pipeline_runs` row per analysis, never overwritten, with the SHA-256 of each weights file,
  the OCR engine that really loaded and per-stage timings (contract 2.1, SQL 011).
- Model registry (champion, candidate, retired per task), dataset versions built from reviewer
  corrections, and a locked evaluation set the database keeps out of training.
- A retry of the same clip resumes after detection from a checkpoint (160 s to 38 s on 30 frames).
- SQL 012 clears the Supabase security advisor warnings (56 to 5).

## Pipeline 3.3, backend 3.2, web 5.2 (8 Oct)

- Detection video and evidence frames draw only the vehicles of record, as translucent shapes
  (silhouettes when the optional segmentation weights are present) with the words outside the box;
  persons, plates, helmets, phones and confidences are no longer drawn.
- A two-wheeler report tracks, judges and reads plates on two-wheelers only; every vehicle is
  still detected for the privacy blur.
- Live progress of a running analysis (stage, frame counter, vehicles so far, percentage,
  estimate) written every 5 s (SQL 013) and shown to reviewers and admins.
- The reviewer queue is grouped by upload; admins can delete a case or every case of an upload,
  audited.
- SmolVLM-500M as an opt-in local tiebreaker backend on the worker's CPU.
