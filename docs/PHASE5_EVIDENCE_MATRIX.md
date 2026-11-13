# Phase 5 — acceptance evidence matrix

Reviewed 2026-11-13. Scope is criterion E in `context/ACCEPTANCE_PLAN.md`.
Phase 5 is in progress. Step 5.1 audited existing evidence; step 5.2 prepared
the local runner and notebook. The physical campaign remains pending. The
physical mapping and centered-source geometry deferred at phase 4 remain out of
scope.

## Existing evidence against criterion E

| Criterion E | Existing evidence | Step 5.1 disposition |
|---|---|---|
| At least 100 consecutive PCM blocks of 48,000 frames | Phase 4.8 has 60 physical blocks, ten in each of six sessions. All used BUILD_ID `B691CC9C9F6249AD938D2063E4767C54`. | Pending. Six sessions do not establish one 100-block campaign. |
| No lost blocks within each capture and zero errors | The 60 phase 4.8 blocks have `(48000,4)` `int32` arrays, exact frame/word/byte counters, and zero clips, overflows, or DMA errors. | Covered for those 60 blocks only; the same checks must pass on all 100 campaign blocks. |
| Recovery after controlled abort and timeout | Phase 4.7 board diagnostics passed abort, recovery, timeout, and the following exact pattern capture. Reprogram/reset recovery also passed. | Covered by the accepted candidate-specific evidence. Do not repeat unless a relevant change or concrete failure gives a reason. |
| Record block times and pauses between calls | Each of the 60 phase 4.8 per-block JSON files records host call start/end UTC. Grouped by the six sessions, call duration was min/median/P95/max `1.015301/1.016159/1.020173/1.023091 s`; 54 within-session pauses were `0.395545/0.414618/0.470826/0.476958 s`. P95 uses nearest-rank. | Partial baseline only. The 100-block campaign must record the same fields and report its own pauses. These are host call times, not acoustic timestamps. A pause between calls is reported separately; it is not evidence of a loss inside a completed block. No continuous acquisition is claimed. |
| Start from a clean kernel and explicitly load the overlay | Phase 4.8 ran the installed notebook in a new kernel per session and explicitly programmed the verified BIT/HWH pair. The exact BUILD_ID and artifact hashes are in its run evidence. | Covered for the accepted candidate. Do not repeat as a standalone check absent a loader/boot change or a concrete failure. |

## Step 5.2 software preparation

Prepared locally on the phase-5 software branch. No board was accessed and no
physical campaign result is implied.

- `pynq_acoustic_camera/stability.py`: fixed 100 × 48,000-frame serial runner,
  candidate identity/provenance checks, per-block sample and JSON persistence,
  exact PCM/counter/status validation, no retries, stop on first failure,
  timing summaries, clock cleanup record and evidence-storage preflight.
- `pynq_acoustic_camera/data/notebooks/05_acquisition_stability.ipynb`:
  package version `0.1.0rc2`, hash-pinned explicit overlay programming, and
  `RUN_CAMPAIGN = False` by default.
- `docs/PHASE5_SOFTWARE_RUNBOOK.md`: execution and evidence interpretation.

Disposition: instrumentation is prepared; the required 100-block physical
campaign remains pending for step 5.3. The pause metric includes host overhead
between API calls and is explicitly not treated as lost samples inside a block
or as proof of continuous acquisition.

## Evidence locations

- Phase 4.7 board summary: `evidence/phase-04-step-07/README.md`
- Phase 4.7 board diagnostics: `evidence/phase-04-step-07/run-20261112T233235Z-173042ec/diagnostics-report.json`
- Phase 4.8 physical summary: `evidence/phase-04-step-08/README.md`
- Phase 4.8 block/tone review: `evidence/phase-04-step-08/board-evidence-review.json`
- Phase 4.8 per-block host timestamps: the 60 `*-repNN.json` files below the six `evidence/phase-04-step-08/run-*/pcm-*/` directories
- Phase 4.8 fresh-kernel runner: `evidence/phase-04-step-08/via_jupyter.py`; each run's executed notebook records explicit programming of the candidate
- Hardware timing and warning review: `evidence/phase-04-step-06/review.json`

## Campaign requirement

The remaining acceptance gap is one serial campaign of 100 physical captures,
each requesting exactly 48,000 PCM frames, with no skipped or hidden-retried
block. Each capture must have exact shape, dtype, counters, and byte length,
with no error flags, overflow, clips, or DMA errors. Record host start/end times
and the finish-to-next-start pause for each adjacent pair. Inter-call pauses are
not counted as missing samples inside a capture, and these block captures do
not certify continuity between calls.

The accepted phase 3 RAW integration lock remains unchanged. Existing phase 4
acceptance and the deferred geometry are not broadened by this matrix.
