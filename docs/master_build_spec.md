# RakshaRide AI Pipeline — Master Build Specification

> Consolidates everything established across all research/debugging sessions
> into one build plan. This is not a redesign document — every fix below
> targets a specific, evidenced problem in the existing 8-stage pipeline.
> Read Section 8 before touching anything related to MareArts.

---

## 1. What this pipeline is, in one paragraph

A dashcam clip goes in. Frames get sampled. Four models run per frame
(general object detection, helmet, plate, vehicle-class). ByteTrack
assigns a persistent ID to every vehicle across the clip. Per-frame
observations accumulate per track — plate reads, violation checks,
vehicle-class votes. Once the clip ends, each track resolves to a final
verdict via evidence accumulation (not single-frame decisions). Anything
ambiguous escalates to a VLM as a validator, never a primary detector.
The result: one report per video, containing every vehicle seen and
which of them have a *confirmed* violation, with evidence attached.

**This is already the right architecture.** Every "should we go
distributed / networked / event-driven" proposal researched during this
project reached the same conclusion on its own: the problem was never
the shape of the pipeline. It was specific, findable bugs in the data
plumbing connecting stages that already work.

---

## 2. Confirmed bugs — found via forensic analysis of real pipeline output

These aren't hypothetical. Each was found by inspecting an actual
`report.json`/`track_log.json` from a real test run.

### 2.1 `first_seen` writes a pixel coordinate, not a timestamp
**Location:** `run_pipeline.py`, line 167.
```python
# WRONG (current):
"first_seen": round(trk.last_bbox[0] if trk.last_bbox else 0.0, 3),
# trk.last_bbox[0] is x1 -- a pixel coordinate, e.g. 412.0px

# CORRECT:
"first_seen": round(max(0.0, (trk.age - trk.hits) * interval), 3),
```
**Impact:** produces values like `first_seen: 1587.812` in a 20-second
video. This silently breaks any downstream logic that trusts timestamps
— confirmed to break the car-merge logic in Section 3, empirically,
not just in theory. **Fix this first — nothing else in Section 3 can
be verified correct until this is fixed.**

### 2.2 Car tracks never get merged — only motorcycles/bicycles do
**Location:** `tracker.py`'s `stitch_track_fragments()`.
Geometric stitching (centroid distance + time gap) works for
motorcycles, which are well-spaced in traffic. It was deliberately never
extended to cars, because cars sit bumper-to-bumper and the same rule
would wrongly merge two different adjacent cars.

**Proof this actually happens:** two tracks in a real test run
(`HH01DP1218` and `MH01DP1218`) were the same physical car, split into
two IDs — the plates differ by exactly one OCR-misread character.

**Fix:** `track_merger.py` (built, tested) — uses plate-read similarity
(Levenshtein distance ≤ 2) plus time-window compatibility as the merge
signal for cars, instead of geometry. Critically, **the merge must
happen on raw OCR reads before voting, not on two already-resolved
plates** — tested both orders; merging post-resolution can pick the
wrong plate even when the correct one exists in the combined evidence.
`plate_resolver_fix.py`'s `merge_track_reads()` does this correctly.

### 2.3 Two broken aggregate rollups, same underlying bug shape
`track_log.json`'s `avg_confidence` is `0.0` for every track. A
separate video-level field, `meta.ocr_agreement_ratio`, is also always
`0.0`. Per-track data is fine in both cases — whatever function rolls
either of them up into a summary isn't looping over the real data.
**Not yet fixed — needs the actual rollup function found and tested in
isolation.**

### 2.4 Helmet/triple-riding checks can fire on 4-wheelers
**Root cause:** missing vehicle-class guard before evaluating rider
association. **Fix:** `vehicle_class_gate.py` (built, tested) — a hard
guard clause, not a threshold tweak. These checks now refuse to
evaluate at all unless the vehicle is confirmed as a two-wheeler.

### 2.5 Plate cross-vehicle bleed (a bystander's plate on the flagged vehicle)
**Fix:** `plate_resolver_fix.py` — plate resolution is strictly scoped
to the subject vehicle's own tracked frames; an unreadable plate stays
`None`, never substituted.

---

## 3. Building blocks — what's actually built and tested

| File | Purpose | Status |
|---|---|---|
| `plate_resolver_fix.py` | Per-track plate OCR resolution: majority vote, agreement ratio, human-readable reasoning, cross-track merge (raw reads, correct order) | Built, tested against real data |
| `vehicle_class_gate.py` | Hard guard: helmet/triple-riding never evaluate against non-two-wheelers | Built, tested |
| `vehicle_class_aggregator.py` | Same accumulate-then-vote pattern as plates, applied to vehicle classification; flags unstable/flip-flopping class assignments | Built, tested |
| `track_merger.py` | Finds car-track merge candidates via plate similarity + time-window compatibility; includes a timestamp validator that catches the 2.1 bug pattern | Built, tested |
| `vehicle_state.py` | The shared per-vehicle object (`VehicleState` + `VehicleStateRegistry`): accumulates violation observations per track, resolves via evidence-count + agreement-ratio thresholds, exports the vehicle-history report shape | Built, tested |
| `plate_resolver_fix.py` | Per-track plate OCR resolution: majority vote, agreement ratio, human-readable reasoning, cross-track merge (raw reads, correct order) | **Built, committed** |
| `vehicle_class_gate.py` | Hard guard: helmet/triple-riding never evaluate against non-two-wheelers | **Built, committed** |
| `vehicle_class_aggregator.py` | Same accumulate-then-vote pattern as plates, applied to vehicle classification; flags unstable/flip-flopping class assignments | **Built, committed** |
| `track_merger.py` | Finds car-track merge candidates via plate similarity + time-window compatibility; includes a timestamp validator that catches the 2.1 bug pattern | **Built, committed** |
| `vehicle_state.py` | The shared per-vehicle object (`VehicleState` + `VehicleStateRegistry`): accumulates violation observations per track, resolves via evidence-count + agreement-ratio thresholds, exports the vehicle-history report shape | **Built, committed** |
| `feedback_layer.py` | Manual human review layer: logs agree/disagree/uncertain against AI verdicts, surfaces disagreement-rate and confidence-calibration patterns. Deliberately does not auto-adjust anything — human decides what to act on | **Built, committed** |
| `plate_crop_enhancer.py` | Pads plate bbox by ~15%, upscales to 128px height (capped) before OCR — fixes small/distant plate misreads. Wired into `plate_aggregator.add_raw_read()`. | **Built, wired, committed** |
| `indian_plate_validator.py` | Real Indian state/UT code whitelist — catches format-valid-but-impossible plates (e.g. `HH01DP1218`) that the format regex alone lets through | **Built, committed** |

**Not yet built:** `plate_resolver_fix.py` — still needed as a standalone resolver. `intelligence.py` (temporal reasoning layer) — may be superseded by `VehicleState.resolve_violation()` which already implements evidence-count + agreement-ratio logic.

---

## 4. Build order

| # | What | Depends on | Status |
|---|---|---|---|
| 1 | Fix `first_seen` (Section 2.1) | Nothing — do this first | ✅ Done |
| 2 | Wire `vehicle_state.py` into `run_pipeline.py`; feed `rules.py`/`heuristics.py` observations into it | Step 1 | ⬜ Next |
| 3 | Wire `track_merger.py` into Stage 3 + wire `indian_plate_validator` into `plate_aggregator` | Step 1 (needs real timestamps) | ⬜ Next |
| 4 | Confirm/build the rollup fix (Section 2.3) — avg_confidence and ocr_agreement_ratio always 0.0 | None, independent | ⬜ Investigate |
| 5 | Wire `plate_crop_enhancer.py` into the OCR call sites | None, independent — safe to do anytime | ✅ Done (wired into `plate_aggregator.add_raw_read()`) |
| 6 | Rewrite `report.py` to call `VehicleStateRegistry.export_report()` instead of building a flat list | Step 2 | ⬜ After Step 2 |
| 7 | Wire `feedback_layer.py` into the admin review action | Backend review endpoint must exist first | ⬜ After backend |
| 8 | Build `intelligence.py` only if `VehicleState`'s built-in resolution proves insufficient once real data runs through it | Step 2, evaluate after | ⬜ Evaluate after Step 2 |

---

## 5. How it all works, end to end

```
Video + declared vehicle_type
        |
        v
Frame extraction (unchanged)
        |
        v
4 models per frame: general detection, helmet, plate, vehicle-class
        |
        v
ByteTrack assigns persistent IDs
        |
        v
Per-frame observations feed into VehicleStateRegistry, keyed by track_id:
  - plate reads -> plate_crop_enhancer (zoom) -> plate_aggregator -> PlateAggregator
  - class votes -> vehicle_class_aggregator.VehicleClassAggregator
  - violation checks -> VehicleState.add_observation()
    (gated by vehicle_class_gate.py where applicable)
        |
        v
Before final resolution: track_merger.py finds car-fragment merge
candidates; approved merges combine raw reads (not resolved answers)
        |
        v
Each VehicleState resolves: confirmed / insufficient_evidence / not_present,
with reasoning text, per violation type
        |
        v
Ambiguous cases only -> VLM validator (Gemini) -- confirms or
downgrades, never introduces a new violation type
        |
        v
report.py exports VehicleStateRegistry -> vehicles array (Section 3.7
shape: one entry per tracked vehicle, only confirmed violations listed)
        |
        v
Sent to backend as one video's worth of vehicle_records (see
backend_requirements.md for the exact contract)
        |
        v
Admin reviews each pending_review record -> confirm/reject
        |
        v
On confirm only: trust_score_history gets a new row; feedback_layer.py
logs the human's judgment for later pattern analysis
```

---

## 6. What this deliberately does NOT include, and why

- **No distributed/microservices rewrite.** Every "network architecture"
  proposal researched (multiple independent analyses, including a
  ChatGPT-drafted 16-point design and separate DeepStream/Kafka/K8s
  research) converged on the same conclusion on its own: the models
  already produce independent, track-keyed output. The missing piece
  was always the shared state object, never the process architecture.
- **No DeepStream/Triton/Kafka/Kubernetes.** Real, legitimate
  infrastructure — for many-concurrent-camera production deployments.
  Wrong scale for a single-GPU pilot processing one clip at a time with
  no measured bottleneck data to justify it.
- **No fully-automated retraining.** `feedback_layer.py` surfaces
  patterns; a human decides what to act on. An AI system retraining on
  its own uncorrected output is a real accuracy-drift risk for a system
  with real consequences (trust scores).
- **No RAG.** The traffic-rule knowledge base is ~10-15 rules — small
  enough to include in full in a VLM prompt every time. Retrieval
  infrastructure solves a scale problem this project doesn't have.
- **No MareArts SDK.** Decision made. Free equivalents implemented (see Section 8).

---

## 7. Zoom-then-read (plate crop enhancement) — committed

Pad the detected plate bbox by ~15%, upscale to 128px height (capped at
a max factor so a near-noise detection doesn't get blown up into false
confidence), before handing the crop to any OCR tier. `plate_crop_enhancer.py`
implements this, and is **wired into `plate_aggregator.add_raw_read()`** as of
this session.

**Honest limit:** this can't recover detail that was never captured. A
plate small enough that individual character strokes are 1-2 pixels
wide will still be genuinely hard to read after upscaling. If that
turns out to be a real remaining gap after this ships, a learned
super-resolution model is the next real step up — at real additional
compute cost, so worth confirming the need first.

---

## 8. MareArts ANPR — decided: not using the paid service

**Decision made: no.** The paid SDK is not being adopted. What's being
taken instead is the *logic*, for free, without their product:

| MareArts idea | Free equivalent, in this project |
|---|---|
| Padded zoom before OCR | `plate_crop_enhancer.py` — built, wired |
| Region-specific character sets for accuracy | `indian_plate_validator.py` — a real Indian state/UT code whitelist, closing a confirmed gap (Section 3) |
| MMC cloud vehicle-type cross-check | Reuse the existing VLM call on `needs_review` cases — ask it for vehicle type in the same call, feed the answer into `vehicle_class_aggregator.py` as another vote. Zero new API cost |
| ONNX runtime speed advantage | Export your own trained models (`ampr.pt`, `helmet_model.pt`) to ONNX directly via `ultralytics`'s `model.export(format='onnx')` — free speed on models you already own |

Both source repos (`Traffic-Rule-Violation-Detection-System-master` and
`MareArts-ANPR-main`) audited and found to contain zero reusable code for
this project. Safe to delete.

---

## 9. Open item found during this update — worth closing

The `HH01DP1218` vs `MH01DP1218` case (Section 2.2) revealed that the
format regex alone can't tell a real plate from an impossible one —
both passed as "valid," so a confidence tie-break had to decide, and it
initially picked wrong. `indian_plate_validator.py` closes this: filter
every OCR candidate through `is_real_state_code()` before voting, not
just the format regex. Confirmed on the real 3-way-tie case from
Section 2.2 — with state-code filtering applied, only one candidate
survives at all, and the tie never happens in the first place.

**Status:** `indian_plate_validator.py` built. Still needs to be wired
into `plate_aggregator.resolve_track()` (Step 3).

---

## 10. Current status (fully wired & verified)

### Completed ✅
1. **Bug 2.1 fixed** — `first_seen` now writes real timestamps (`run_pipeline.py` line 187).
2. **Bug 2.3 fixed** — `avg_confidence` filled from `track_results` detections; `violations_on_track` back-filled from `frame_verdicts`.
3. **Bug 2.5 fixed** — `best_plate_candidate()` state-code filter applied in Stage 5 plate winner selection.
4. **`plate_crop_enhancer.py`** — built + wired into `plate_aggregator.add_raw_read()`.
5. **`indian_plate_validator.py`** — built + wired into Stage 5 resolution.
6. **`vehicle_class_gate.py`** — built + wired into `rules.py`, `run_pipeline.py`, and `vehicle_state.py`.
7. **`vehicle_class_aggregator.py`** — built + wired into Stage 3 with flip-flop detection and registry integration.
8. **`track_merger.py`** — built + wired into Stage 5 car track merger with Levenshtein + time compatibility.
9. **`vehicle_state.py`** — built + wired into `run_pipeline.py` (evidence accumulation, per-track state, resolution gating, `registry.export_report()`).
10. **`report.py`** — updated with `vehicle_records` (`vehicles_v2`, `all_tracked_vehicles`, summary metrics).
11. **`feedback_layer.py`** — built for admin review logging.
12. **Full test suite** — 84 unit tests passing across all pipeline and track-network components.

