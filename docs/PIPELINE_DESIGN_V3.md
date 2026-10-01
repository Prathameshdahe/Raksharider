# RoadWatch.AI — Model Pipeline v3 design (accuracy first)

Goal: every flag belongs to one identifiable vehicle, is backed by frames you can show,
and reaches a human only when the machine is genuinely unsure. Speed is not a goal;
clips are batched and processed within days, so we spend compute on accuracy.

Decisions already made
- Tiebreaker: Gemini (vision) with Nemotron via NVIDIA NIM as fallback. External calls
  are bounded, logged, and can never override a multi-frame confirmed finding.
- Roles: Citizen uploads, Reviewer decides, Admin runs the queue. The pipeline never
  changes a trust score; only a reviewer's confirmation does.
- Contract: `pipeline/contract.py` is the only schema between pipeline and worker.

---

## 1. Flow

```
 video ─► 0 Ingest ─► 1 Dense detect+track ─► 2 Keyframes per track ─► 3 ROI analysis (zoomed crops)
                                                                             │
        9 Review loop ◄─ 8 Persist ◄─ 7 Evidence+report ◄─ 6 Auto-score tiers ◄─ 5 Per-vehicle verdicts
                                                                             ▲
                                                4 Plate OCR + identity ledger ─┘  (+ 5b VLM tiebreaker, bounded)
```

Everything after stage 1 is **per vehicle track**. Nothing is ever computed "for the frame"
and then handed to whichever vehicles happen to be visible.

---

## 2. Stages

### 0. Ingest
- Probe fps, resolution, duration. Decode **every frame** (or every 2nd frame above 30 fps).
  Keep original frame index and real timestamp on every `Frame`.
- Why: ByteTrack needs consecutive boxes to overlap. At 0.5 s spacing a moving two-wheeler
  has zero overlap with its previous box, so every sample became a new ID (your run: one
  motorcycle → IDs 31, 33, then lost).

### 1. Dense detection + tracking (identity layer)
- COCO/vehicle model on the dense frames at `imgsz=1280` (4K source; 640 turns a rider's
  head into ~10 px). Tiled inference is the fallback if 1280 still misses far vehicles.
- ByteTrack + Kalman as today, now fed at 15–30 fps instead of 1–2 fps.
- **Track life gate**: a vehicle exists only after ≥3 hits spanning ≥0.5 s. One-frame flickers
  (6 of 25 records in your run) never become records.
- **Appearance signature** per track: colour histogram of the vehicle crop, updated each hit.
  Used only to re-link fragments across gaps ≤1 s together with motion; never to create identity.
- **Rider association** per frame (existing geometry + contested-rider tiebreak). A person track
  is attached to a motorcycle track; persons are never vehicles.
- Only vehicle-class tracks enter the registry.

### 2. Keyframe selection (per track)
- For each vehicle pick K=5 frames: largest box × sharpness (Laplacian variance), spread in time.
- These are the frames the expensive models look at, and the frames that become evidence.
  Global "top-3 frames of the clip" is gone.

### 3. ROI analysis on zoomed crops
For each keyframe of each vehicle, crop the vehicle (+15 % margin), upscale to 640, and run:
- helmet model on each attached rider's head region → `helmet | no_helmet | unclear`
- phone (COCO cell_phone) on rider / driver region
- plate detector on the vehicle crop
- wheelie: aspect-ratio time series over the dense track (review-only, needs ≥4 consecutive frames)
- erratic driving: trajectory with ego-motion compensation (existing)

Every check writes a **three-state observation** to the track: positive / negative / not evaluable.
`no_helmet` needs a positive `no_helmet` box on the head. Absence of a detection is "unclear".
Why crops: the helmet model fired **zero** times on your 4K clip at full-frame 640 inference
while the rider at 16 s visibly wears a helmet.

### 4. Plate reading + identity ledger
- OCR only on plate crops from that track's keyframes: EasyOCR → PaddleOCR → (VLM tier only if `--vlm`).
- Accept a plate only if: Indian format, real state code, confidence ≥ 0.6, ≥2 agreeing reads
  (or 1 read + VLM concurrence). Otherwise the plate is `None` and the record is marked
  needs-review; never a nearby guess ("MH1R1077" at 24 % in your run would be rejected).
- Identity ledger per track: plate reads, appearance signature, motion continuity → status

| status | meaning | effect |
|---|---|---|
| resolved | validated plate, consistent | may auto-flag |
| provisional | no plate; motion+appearance unambiguous | may flag, "identity unverified" |
| ambiguous | look-alike vehicle nearby, no plate | never auto-flags; human picks |
| conflict | one track read two different valid plates | split the track at the change |

- Same validated plate on two **non-overlapping** fragments → canonical merge (one helper
  updates OCR, registry, history, labels). Same plate on two **overlapping** tracks → conflict, no merge.

### 5. Per-vehicle verdicts
- Dedup to one observation per (violation, frame). Agreement = positive / evaluable frames.
- Policy table (`violation_policy`): enabled, two-wheeler-only, review-only, severity.
- Results: `confirmed | needs_review | observed_absent | unobservable | not_evaluated`.
- Clip status is **derived** from the vehicles (auto_flagged if any confirmed, else needs_review
  if any review, else insufficient_evidence).

### 5b. Tiebreaker (Gemini → Nemotron), bounded
- Runs only with `--vlm`, only for `needs_review` findings on flagged vehicles, max 3 calls per clip.
- Input: that vehicle's best evidence crop + structured summary. Output is written back as a
  VLM observation on that track and the track re-resolves. Every call logged (scope, output,
  model, error). API down or quota gone → finding stays `needs_review`.

### 6. Auto-score and tiers (the automation gate)
Per finding, from: evidence frames, agreement, detection confidence, plate identity status, VLM concurrence.

| tier | condition (initial values, tune from reviewer decisions) | outcome |
|---|---|---|
| A auto-flag | ≥3 positive frames, agreement ≥0.7, conf ≥0.6, identity resolved/provisional, VLM not contradicting | queued as high-confidence |
| B review | anything positive that misses A, or identity ambiguous | queued as normal review |
| C drop | single low-confidence frame, or unobservable | kept in report for audit, not queued |

Identity ambiguous forces tier B no matter how strong the behaviour evidence is.

### 7. Evidence + report
- Per vehicle: its keyframes with positive observations, subject box highlighted, plus head and
  plate crops. Paths `tracks/<track_id>/t<ts>.jpg`, uploaded as `{video}/{run}/{track}/<file>`.
- Report = list of `VehicleRecord` + derived clip summary. Invariants asserted
  (`0 ≤ first ≤ last ≤ duration`, distinct evidence frames, every violation has a track).

### 8. Persist
- Worker: vehicle_records + violations + video status in one transaction; failure → `failed` + reason.

### 9. Review loop → labels
- Reviewer confirms/rejects with reason, corrects plate/type. Stored in `audit_log`.
- These decisions are the labels: (a) tune tier thresholds, (b) mine hard cases for retraining the
  helmet and plate detectors. Retrain only after the evaluation set below exists.

---

## 3. Evaluation harness (non-negotiable)
- 10 clips, per-frame boxes + IDs labelled in CVAT, violations and plates marked by hand.
- Metrics per change: IDF1 / MOTA (tracking), per-violation precision & recall, plate exact-match
  rate, **wrong-vehicle attribution rate**, time per clip.
- Every threshold in stage 6 is tuned against this set, never by eye.

---

## 4. Compute budget (20 s, 4K, 30 fps → ~600 frames, RTX 3060)
| step | estimate |
|---|---|
| COCO at 1280, 600 frames | ~15–25 s |
| crops: ~25 vehicles × 5 keyframes × 3 models | ~10 s |
| OCR on plate crops | ~30 s |
| VLM (≤3 calls) | ~10 s |
| total | ~1–2 min per clip on GPU; ~10–15 min on CPU |
Fine for a queue that delivers results within days.

---

## 5. Build order
1. Ingest dense frames + keyframe selector (frame_extractor, new `keyframes.py`)
2. Detector `imgsz` + ROI crop runner (detector.py)
3. Track life gate + appearance signature + gap re-link (tracker.py, registry)
4. Plate confidence floor + identity ledger + split-on-conflict (plate_aggregator, vehicle_state, track_merger)
5. Auto-score tiers (vehicle_state → contract, worker writes tier to vehicle_records)
6. Evaluation harness + first labelled clips (tests/eval/)
7. Only then: retrain helmet/plate models on reviewer-mined hard cases

Each step keeps the 115 existing tests green and adds its own golden test.
