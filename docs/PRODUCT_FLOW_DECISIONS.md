# RoadWatch.AI — Product flow and model pipeline (decisions record, revision 2, 17 Sep 2026)

Single source of truth agreed in the walkthrough, then hardened by an adversarial design
review (63 findings, 62 upheld). Numbered items are decisions in the order taken;
"(default)" marks a proposed default the user accepted; "(review)" marks a rule added or
changed by the review. Nothing here is code; it is what the code must do.

Guiding rules (apply everywhere)
- **Accuracy over speed.** Minutes per clip are fine. Frames, resolution and evidence are never
  traded for time. "Accuracy" is measurable — see §4.12.
- **AI proposes, reviewers decide, authorities enforce.** No finding counts until a reviewer confirms
  it. RoadWatch never issues or collects a fine. Nothing the AI outputs is a decision.
- **Every finding belongs to one vehicle**, carries its own evidence, and survives storage and
  retries unchanged.
- **Role checks live in the backend and the database.** Frontend navigation is convenience.
- **The uploader's claim is an allegation, never evidence** (review). It selects the subject and sets
  priority; it never validates a plate or a finding.
- RoadWatch cannot verify that footage is unedited or unsynthesised; the upload attestation is a
  declaration, one more reason no finding counts without a human (review).

---

## 0. Legal basis, third parties, retention (review)

A vehicle registration number is personal data (linkable to an owner through the RTO). RoadWatch
processes it only for traffic-safety reporting, never resolves a plate to a name or address, and
keeps it for a fixed period. Third parties in a clip (riders, drivers, pedestrians) never consented:

| rule | value |
|---|---|
| evidence images | **citizens never receive evidence images at all** (integration review): they get the per-finding text outcome and the redacted detection video. Reviewers and admins get the evidence images, unblurred, because a blurred face makes a helmet undecidable |
| faces of every person | blurred in the detection video, the only image artefact a citizen receives |
| vehicles that are not the subject, in the detection video | the plate region of every such vehicle is blurred whether or not a plate was detected, and the windscreen band of every four-wheeler is blurred. Over-blurring a stranger's vehicle is the safe direction; a missed plate is not |
| plates of non-subject vehicles | blurred in the detection video, always (hard rule, not a default) |
| the subject's plate in the uploader's copy | readable only if the uploader typed it AND identity resolved with an exact match; otherwise blurred |
| unconfirmed observations | 90 days, then aggregated to counts with no evidence |
| confirmed findings + evidence | 2 years |
| original clips | 30 days after the last case on the run is finalized (withdrawn: 7 days) |
| escalation packages | until the admin marks them delivered + 1 year |

Escalation is not automatic reporting: it produces a package an authorised officer may forward; the
authority decides independently. For the student deployment: reviewers are project members and
mentors acting as stand-ins, no real officers are enrolled, and escalation is archive-only until an
authority agreement exists.

---

## 1. Roles

| Role (DB value) | Label | Can | Cannot |
|---|---|---|---|
| citizen | User | upload clips, see own submissions and every review on them, withdraw while allowed, receive alerts | see other people's plates or unblurred evidence, search histories, review anything |
| officer | Reviewer | claim up to 3 cases, see full evidence and plate reads, decide per finding, correct plate / type / label / evidence with reasons, submit for second opinion, finalize, release own claim | edit another reviewer's claim, reopen, grant roles, overwrite AI output, change settings, see a plate's history before finalizing |
| admin | Admin | approve reviewers, live board, processing operations, reopen with reason, plate history, escalation approval, audit, settings | act as a reviewer except through the audited override |
| worker | AI worker (service identity) | claim jobs, heartbeat, upload evidence, submit one result package per run through `persist_run_result` | write to any table directly; touch users, roles, decisions, cases, audit, scores, settings |

Signup always creates **citizen**. "I am a traffic officer" records a request with a badge number.
Reviewer approval (23, review) requires the badge number, an official department e-mail or a
supervisor's letter, and an out-of-band confirmation the admin records in `verified_via`. Admins
are provisioned by script. A demoted or deactivated reviewer's open claims are released to the queue
and their pending second opinions reassigned (review).

Worker grants (review): the worker role has EXECUTE on `claim_next_video()` and
`persist_run_result(jsonb)` plus SELECT on `videos` and UPDATE on `system_settings` for the
heartbeat. The direct table grants from migration 002 are revoked by migration 003.

---

## 2. The flow, step by step

### 2.1 Landing and login
1. **Public landing page** first: what RoadWatch does, how a clip is handled, one safe sample with
   fake plates, privacy note, Sign in / Create account. Never real plates, evidence, queue,
   histories or internal errors.
2. **One login for all roles**; the backend returns the role and the portal. The user portal opens
   on the upload action.

### 2.2 Upload
3. One form, vehicle type radio first (**two-wheeler / four-wheeler**), pick-list filtered by type
   (review). The uploader saw bad behaviour from the vehicle in front of them.
4. Fields: file, **what they think should be flagged**, a **2–3 line note**, **plate if known**
   (optional), recording source (dashcam / phone / CCTV / other), recording date, rough location or
   GPS (optional), and the attestation that the footage is genuine and theirs to submit (review:
   source and GPS reinstated from the backend document).
5. Browser uploads directly to private storage via a short-lived signed URL; the backend verifies
   type, size, ownership and records the file's sha256. A byte-identical file already submitted by
   anyone is refused as a duplicate (review).
6. Quota 10 uploads per user per day, 200 MB per file. When intake is paused the upload is accepted
   and shows "Queued (intake paused)"; it is never rejected (review).

### 2.3 Queue and priority
7. **Priority = seriousness of the declared violation** (§5 tiers), then age. No days-long batching.
8. Ordinary submissions appear on the live board; only top-tier ones raise an admin alert (24).
9. Leased jobs with heartbeats; a silent worker's job returns to the queue; three attempts then
   permanently failed with an **error category** (download / pipeline / contract / persist);
   download and persist failures are retryable, contract failures are not (review).

### 2.4 What the AI does with the claim
10. The declared vehicle and violation define **the subject of the case**. Every other vehicle is
    still analysed; anything found becomes a **secondary finding** on its own case (5, 8).
11. **Subject selection never reads findings** (review): (a) the track whose *resolved* plate equals
    the claimed plate; else (b) the most prominent track of the declared vehicle type (box area ×
    time, weighted to the lower centre of the frame) if it beats the runner-up 2×; else
    (c) `ambiguous_subject` with the tied tracks listed (reviewer picks, normal lane); no track of the
    declared type at all → `not_supported: declared vehicle not seen`.
12. The uploader's plate is a **claim, not truth** (review, tightened): a plate is *resolved* only
    from two agreeing OCR reads on two different frames at ≥ 0.6 confidence with a real state code.
    The claim is compared afterwards: exact match keeps "resolved"; one character off is a
    **conflict** (0/O, 8/B, 1/I are exactly the errors a typist and an OCR share); one read that
    equals the claim is "provisional, ocr+claim" and never tier A. Two different plates on one track
    is a conflict; the reviewer settles conflicts by a plate correction with a reason.
13. **The allegation is answered from the subject's verdict** (review): `supported` (confirmed or
    needs_review), `not_supported` only when the violation was *observed absent* in ≥ 3 frames over
    ≥ 1 s, `unobservable` otherwise, `manual_review` for violation types the AI cannot evaluate yet
    (red light, wrong side, seatbelt, lane cutting — the reviewer decides from footage, tier B,
    normal lane), `ambiguous_subject`, or `not_declared`.
14. "AI: allegation not supported" is a **routing label, not a verdict** (review). It goes to a
    **reviewer lane** (the admin sees the count; an admin may take it only through the audited
    override). The worker always emits one finding for the declared violation on the subject with
    the counter-evidence frames; the reviewer sees the uploader's note and frames before the AI's
    label, shown as "AI could not confirm — see frames". When no subject exists the case has no
    vehicle observation and the only decisions are rejected or inconclusive.

### 2.5 Cases and the reviewer
15. One **case per vehicle track per run**. Never two cases for the same vehicle in one clip.
16. **Claim** locks the case to the reviewer (max 3 open). A claim idle 24 h (no decision or
    correction saved) alerts the reviewer; at 48 h it auto-releases with an audit row; the reviewer
    may release their own claim (review). Admin may release or reassign any claim.
17. **Decisions are per finding**: confirmed / rejected / inconclusive (default). Tier C findings are
    stored on the case with decision `not_queued`, hidden by default, expandable (review).
18. **Rejections use predefined reasons** (helmet_worn, wrong_vehicle, plate_misread,
    footage_unclear, not_a_violation, duplicate_case, other) with an optional note (10). Rejected
    findings are never deleted (11). A finding rejected as `wrong_vehicle` or `plate_misread`
    creates **no** history row on the AI's plate (review).
19. **Inconclusive** is per finding (review, mechanics): when every finding is decided and any is
    inconclusive, the button is "Submit for second opinion": status `second_opinion`, claim cleared,
    `first_reviewer_id` recorded; the claim query excludes the first reviewer; reviewer 2 decides
    only the inconclusive findings, sees reviewer 1's decision values but not notes; reviewer 2's
    confirmed/rejected is final; a second inconclusive becomes `unverifiable`; the case then
    finalizes automatically.
20. **Corrections**: plate, vehicle type, violation label, unselecting evidence frames. AI values are
    never overwritten; the correction sits beside them. Reason mandatory for rejections and plate
    corrections. A label correction moves the finding to the corrected type's tier and it counts
    under the corrected label (review). A finding cannot be confirmed with zero selected evidence
    frames (review).
21. **Finalize** locks the case; only an admin can reopen, with a reason. A reopened case goes back
    to the queue as a new cycle with the original decisions kept read-only; the original reviewer is
    excluded from claiming it (review).
22. Before deciding, the case screen shows only a plate hint: "N other open or finalized cases on
    this plate in the last 24 h" (no details) so `duplicate_case` is usable; full history after
    finalize (review).

### 2.6 What the uploader sees
23. Plain-word statuses: Uploading, Queued, Queued (intake paused), Analysing, Processing delayed
    (retryable failure or older than 30 min in queue), Awaiting review, Decided, Withdrawn,
    Could not process. **Decided** only when every case from the run is finalized; second opinion
    still shows Awaiting review; `unverifiable` shows per finding as "Could not be verified" (review).
24. Once processed the uploader sees **every review on their submission**, finding by finding, with
    the rejection reason (17). Secondary findings appear as "Vehicle #2: no helmet, confirmed" with
    no plate.
25. Citizens receive **no evidence images**; the detection video plus the per-finding text is their
    whole view of the analysis (integration review). After review they receive the **detection video** (18): one per run, rendered by the worker at
    processing time under `{submission}/{run}/detection.mp4`, delivered through a short-lived
    signed URL after the subject case is finalized, kept for the evidence retention period,
    rendered regardless of outcome. Faces blurred, non-subject plates blurred, the subject's plate
    per §0. Reasoning overlays only on the subject's positive frames (review).
26. **Withdrawal** (review): instant while no case of the run is claimed (a queued job is cancelled,
    a running job finishes and its results are discarded); once any case is claimed it becomes one
    request per submission that the claiming reviewer answers; declined shows "Withdrawal declined"
    with the reason; secondary cases withdraw with the submission unless already claimed. Withdrawn
    material keeps only unconfirmed plate observations for 7 days.

### 2.7 Plate monitoring and escalation
27. History has **two layers** (25): unconfirmed AI observations exist only for model-quality
    analysis and duplicate detection, never for prioritisation, escalation or display during
    review; the plate's status is `clean` regardless of unconfirmed count (review). Only resolved
    plates, or reviewer-corrected plates, create history rows; raw reads stay in
    `plate_observations`; on correction both layers attach to the corrected plate (review).
28. A finding may be confirmed with no plate; it is stored with plate null and counts toward nothing
    (review).
29. **Escalation** (19, 20, review): counted per distinct (plate, violation, recording date) on
    finalized confirmed findings; threshold three of any kind or one top-tier; waits for admin
    approval; admin may decline with a reason; a reopen-then-reject on a counted case decrements
    and withdraws an undelivered escalation; an escalated plate resets to `watch` with a fresh
    counter once the admin marks the package delivered. Package = ZIP of a PDF summary plus the
    evidence videos, downloaded by the admin from the plate page. It contains reviewer badge
    numbers only for reviewers whose badge was verified (§1).
30. The admin sees plate history; reviewers only after finalizing the current case (20, 27).

### 2.8 Admin
31. Live board: users online (distinct accounts with a request in the last 5 min via `last_seen_at`),
    uploads today, queue depth by priority, clips processing with elapsed time, worker heartbeat,
    failures today by category, review backlog and oldest waiting case, reviewers with a case
    in_review (review).
32. Processing ops, case admin (reopen, reassign, release), model quality (rejection reasons over
    time, AI-vs-reviewer agreement per violation, plate misread rate, not-supported rate, blind-audit
    disagreement), users (per-uploader submissions / confirmed / rejected / not-supported counts; a
    high rejection rate lowers that uploader's priority and may suspend uploads), audit, settings
    (thresholds, quota, retention, pause).
33. **Blind audit** (review): every twentieth finalized case is re-reviewed by a second reviewer who
    sees the raw clip and no AI output; the disagreement rate is the anchoring measure on the
    quality page. Thresholds move only when the evaluation set (§4.12) and the blind audit agree in
    direction.

### 2.9 Alerts (23–26)
| Who | Immediate alert on |
|---|---|
| Uploader | received, analysed, decided, withdrawal answered |
| Reviewers (broadcast) | new top-tier case, second-opinion case entered the queue, own claim idle 24 h, withdrawal request on my claimed case |
| Admin | top-tier submission, processing failure, worker silent 10 min, reviewer request pending, escalation threshold reached, stale claim auto-released |
One notification per event, no digest.

---

## 3. Statuses (separate columns, never one)

| Thing | Values |
|---|---|
| Submission (`videos.status`) | uploading, unprocessed (= queued), processing, processed, failed, withdrawn |
| Processing job | attempts + error_category on the video row; a separate `processing_jobs` table is a later split |
| Case | pending_review, in_review, second_opinion, finalized, reopened, withdrawn |
| Finding decision | pending, not_queued, confirmed, rejected, inconclusive, unverifiable |
| Plate | clean, watch (confirmed below threshold), escalated |

---

## 4. Model pipeline (accuracy first) — implemented in v3.0.0

The pipeline receives one submission (file + claim) and returns one **result package** (§6). It
never creates decisions. What the uploader's inputs are used for:

| input | used for | never used as |
|---|---|---|
| vehicle type | subject selection only; rules run per *detected* class on every vehicle | a rule selector |
| declared violation | priority; the allegation answered on the subject | evidence |
| claimed plate | compared with the resolved plate afterwards | plate of record, or a second OCR read |
| note | shown to the reviewer | model input |

### 4.1 Ingest: every frame
All frames streamed (every 2nd above 30 fps, target 15 fps), one frame in memory at a time, real
timestamps. Sparse 0.5–1 s sampling is why one motorcycle became three IDs: consecutive boxes never
overlapped.

### 4.2 Detection at the resolution the objects need
Vehicles and people on the full frame at inference size 1280. Heads, phones and plates are
re-detected on **zoomed crops** of each vehicle (helmet on two-wheeler crops extended upward,
plate on every vehicle crop, phone on person crops). The helmet model fired zero times on the
user's 4K clip at 640; whether crops fix that is measured on the evaluation set, not assumed.

### 4.3 Tracking: BoT-SORT, one tracker per class group (measured fix)
The detector runs once per frame; detections are split into two-wheelers and four-wheelers, and each
group has its own BoT-SORT instance with its own Kalman states and camera-motion compensation.

**Why**, measured on the sample clip: Ultralytics associates detections to tracks without regard to
class. Driven the usual way, the one motorcycle in the clip inherited a different car's id in almost
every frame (13, 11, 10, 6, 5, 3, …) as it passed between cars, so it never became a vehicle at all
and the uploader's two-wheeler allegation was answered "no vehicle of the declared type was found".
With class-separated trackers the same motorcycle holds **one id for 61 consecutive frames over 4 s**.
Class-agnostic association is the largest identity error on dashcam footage and is invisible unless
one object is traced frame by frame.

Appearance re-identification is **off** on this path: the ReID encoder needs the predictor's feature
maps, which do not exist when the tracker is driven with pre-computed boxes. Motion + IoU + camera-
motion compensation only. Appearance ReID is a measurable future lever, not a claim.

Track life gate: a vehicle exists after ≥ 3 hits over ≥ 0.5 s. Persons attach to motorcycles as
riders per frame; they are never vehicles and are not tracked. Look-alike = same wheel class, similar
size, boxes practically overlapping; a lane neighbour in a jam is not a look-alike.

### 4.4 Plate-anchored identity ledger
**Ownership by construction** (measured fix): the plate detector runs on each vehicle's own zoomed
crop, so a plate found in vehicle V's crop belongs to V; a plate landing in the crop's padding
(outside V's real box) is discarded rather than guessed. The previous approach — detect plates on the
full frame, then assign by box overlap, plus a guessed "plate region" inside each vehicle box —
attached one physical plate (MH01DP1218) to four different tracks on the sample clip, because in a
jam a plate lies inside many overlapping vehicle boxes. Overlap matching and region guessing are both
removed.

OCR (EasyOCR, PaddleOCR) on the best three plate crops per track after the stream. Identity per
track: `resolved` (two reads, two frames, ≥ 0.6, real state code), `provisional`, `ambiguous`
(look-alike and no plate), `conflict` (two plates, or claim mismatch). Same resolved plate on two
non-overlapping tracks ≤ 5 s apart → one vehicle; overlapping → conflict, never merged.

### 4.5 Per-vehicle observations
Every check writes a **three-state observation** per frame: positive, negative, not evaluable.
`no_helmet` needs a positive no-helmet box on the head. Wheelie and erratic driving are
review-only (geometry alone never confirms). Red light stays disabled.

### 4.6 Verdicts — thresholds are in TIME, not frames (review)
One observation per (violation, frame). Confirmed needs ≥ 3 positive frames **spanning ≥ 1 s** at
≥ 55 % agreement over evaluable frames; three positives inside 0.1 s at 30 fps is one flicker.
`observed_absent` needs ≥ 3 negative frames spanning ≥ 1 s, else `unobservable`. Results:
confirmed, needs_review, observed_absent, unobservable, not_evaluated. The clip status is derived
from the vehicles.

### 4.7 Tiebreaker: Gemini, then Nemotron via NVIDIA NIM (review)
Optional, non-authoritative. One question per needs_review finding on the subject, at most three
calls per clip (highest tier first), input = the subject's crop with faces blurred and non-subject
plates masked. The answer is written back as an observation on that one violation and that one
frame; it can never touch a multi-frame confirmed finding. Every call is logged (model, scope,
output, error). Provider terms: use only tiers with no-training / zero-retention; cost bounded at 3
calls × price, stated per 1000 clips on the settings page. The VLM's agreement with the eventual
reviewer decision is a quality-page metric.

### 4.8 Triage tiers (review: renamed from "automation gate")
Nothing is automated beyond ordering and routing. From evidence frames, agreement, confidence,
identity status, VLM concurrence:

| tier | condition | effect |
|---|---|---|
| A | confirmed, ≥ 3 frames, agreement ≥ 0.7, confidence ≥ 0.6, **identity resolved**, VLM not contradicting | sorts first within its violation tier |
| B | any positive that misses A; any finding on an unresolved / ambiguous / conflict identity; the declared violation always | normal review |
| C | observed_absent, unobservable, single low-confidence frame | stored with decision `not_queued`, hidden by default |

Automated: detection, tracking, plate reading, per-frame observation, allegation routing, evidence
extraction, priority. Never automated: any decision, any plate of record, any escalation, any
message to the uploader beyond status.

### 4.9 Evidence and the detection video
Per **finding** (not per track): up to three keyframes where that violation was observed, subject
box highlighted, sha256 recorded, paths `tracks/{track}/{violation}/t{ts}.jpg` under
`{submission}/{run}/`. Positive frames are always kept as keyframes; counter-evidence frames for
the declared violation too. Detection video per §2.6 item 25.

### 4.10 Compute, measured (review: derived, not asserted)
This machine, CPU only, Python 3.13, Ultralytics 8.4.19, 4K clip:

| stage | 40 frames @ 5 fps, imgsz 960 |
|---|---|
| decode + detect + track + crops + rules | 28.9 s (0.72 s/frame) |
| OCR (EasyOCR, 42 crops) | 127.3 s (dominant on CPU) |
| detection video | 2.8 s |
| findings + evidence | 1.3 s |
| total | 168.5 s |

Full-clip numbers at the production settings (15 fps, imgsz 1280) are appended to this table by the
run report. Deployment target: the worker runs on the lab machine with the RTX 3060 (Docker image
is CPU-only and is a fallback with `--target-fps 5 --imgsz 960`; its accuracy cost is measured on
the evaluation set before it is used for real submissions). Throughput sanity: at 10 uploads per
user per day and N users the queue must clear in 24 h; the live board shows when it does not.

### 4.11 Thresholds and how they move
Initial: 3 frames over 1 s, agreement 0.55 (confirm) / 0.70 (tier A), plate floor 0.6, track life
3 hits / 0.5 s, erratic reversals 3 over ≥ 2.5 s with swings ≥ 0.5 box widths. Reviewer rejections
say *which* hard cases to label next; thresholds change only against the evaluation set.

### 4.12 Accuracy, defined (review)
| metric | definition | target |
|---|---|---|
| wrong-vehicle attribution rate | confirmed findings the reviewer moved to another vehicle / confirmed | < 5 % |
| per-violation precision | confirmed / (confirmed + rejected), tier A held separately | ≥ 0.8 no_helmet, ≥ 0.6 phone_usage, tier A ≥ 0.9 |
| plate exact-match rate | resolved plates equal to hand-labelled plates | ≥ 0.85 at floor 0.6, false accept ≤ 2 % |
| tracking | IDF1 / MOTA on labelled clips | IDF1 ≥ 0.75 |
| review time | median reviewer seconds per case | falls as levers land |

Evaluation set: ≥ 30 clips stratified across two/four-wheeler, day/night, near/far, urban/highway,
≥ 50 labelled instances per enabled violation and ≥ 100 labelled plates, labelled in CVAT, 10 clips
held out and never used for tuning; every metric reported with a 95 % Wilson interval. Until it
exists the levers in §4.1–4.5 are **proposed** (dense sampling is the one already justified by the
observed three-ID failure), and no retraining happens.

---

## 5. Violations by vehicle type and seriousness (21, 22)

| tier | two-wheeler | four-wheeler | effect |
|---|---|---|---|
| top | wheelie, phone use, wrong side*, red light* | phone use, wrong side*, red light* | jump the queue; escalate on one confirmed |
| middle | no helmet, triple riding, lane cutting*, erratic driving* | lane cutting*, erratic driving* | normal priority |
| minor | missing plate | missing plate, no seatbelt* | lowest priority |
\* reportable but manual-review-only: the AI emits `not_evaluated` → `manual_review`, tier B, normal lane.

**Erratic driving was disabled** (measured): from a moving dashcam, lateral motion is dominated by
ego-motion and parallax — vehicles at different depths shift at different rates — and median
compensation does not remove parallax. On the sample jam clip the heuristic flagged 21 of 23
vehicles. Flooding the queue with false cases costs more reviewer trust than the feature is worth.
It returns only with proper ego-motion estimation and a measured false-positive rate on the
evaluation set.

---

## 6. Result package (contract 2.0, `pipeline/contract.py`)
run_id (deterministic per video attempt), submission_id, pipeline + model versions, timestamps,
duration; `allegation` {declared_violation, claimed_plate, vehicle_type, subject_track_id,
subject_method, subject_candidates, answer, reason}; `vehicle_tracks[]` (VehicleRecord: identity
status, plate {text, confidence, method, raw_reads}, verdicts with tier, evidence, is_subject);
`plate_observations[]`; `findings[]` (deterministic finding_id, result, tier, evidence paths,
vlm_call_id); `evidence[]` (finding_id, frame, timestamp, path, sha256); `vlm_calls[]`; `summary`
with per-stage timing; `detection_video`. Guarantees: plates per track only; every finding has a
track; evidence linked to its finding; blob names `{submission}/{run}/{path}` cannot collide; the
same run_id persisted twice is a no-op; the run is complete only when `persist_run_result` commits;
no decisions inside.

---

## 7. Data model (backend/API.md v3 §9)
videos (+ claim fields, priority, error_category, allegation_answer, detection_video_path) ·
plate_observations · cases · findings · finding_decisions · case_corrections · evidence ·
rejection_reasons · plate_history + plate_status view · escalations · withdrawal_requests ·
notifications · audit_log · system_settings · violation_policy (tiers live here).

## 8. Open points
- DB role rename `citizen/officer` → `user/reviewer` (cosmetic, migration).
- Original videos: Azure Blob (backend document) vs Supabase Storage (built today). One must be
  chosen before the upload code changes; the API only ever stores blob paths, so either works.
- Duplicate detection by perceptual hash for re-encoded clips (only byte-identical duplicates are
  refused today).
