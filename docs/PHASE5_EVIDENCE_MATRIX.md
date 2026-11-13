# Phase 5 — acceptance evidence matrix

Reviewed 2026-11-13. Scope is criterion E in `context/ACCEPTANCE_PLAN.md`.
Phase 5 is in progress. Steps 5.1 and 5.2 audited/prepared the procedure, and
step 5.3 completed the 100-block physical campaign. The
physical mapping and centered-source geometry deferred at phase 4 remain out of
scope.

## Existing evidence against criterion E

| Criterion E | Existing evidence | Acceptance disposition |
|---|---|---|
| At least 100 consecutive PCM blocks of 48,000 frames | Phase 4.8 has 60 physical blocks, ten in each of six sessions. Step 5.3 adds one serial campaign of 100 captures under BUILD_ID `B691CC9C9F6249AD938D2063E4767C54`; see `evidence/phase-05-step-03/board/pcm-stability-20261113T184651Z-45725f00/campaign-report.json`. | Passed: one campaign contains 100/100 serial capture calls. This is not a continuous-acquisition claim. |
| No lost blocks within each capture and zero errors | The 60 phase 4.8 blocks have `(48000,4)` `int32` arrays, exact frame/word/byte counters, and zero clips, overflows, or DMA errors. Step 5.3 reloaded all 100 saved arrays and checked shape, dtype, payload size, signed-24 range and each block's exact counters; all passed with zero validation errors. | Passed for the 100 step 5.3 blocks. The post-transfer hash inventory is `docs/PHASE5_CAMPAIGN_AUDIT.json`; raw arrays and records remain in workspace evidence. |
| Recovery after controlled abort and timeout | Phase 4.7 board diagnostics passed abort, recovery, timeout, and the following exact pattern capture. Reprogram/reset recovery also passed. | Covered by the accepted candidate-specific evidence. Do not repeat unless a relevant change or concrete failure gives a reason. |
| Record block times and pauses between calls | The 100-block report records UTC and monotonic host call starts/ends and all 99 finish-to-next-start pauses. Call duration min/median/P95/max: `1.026908/1.046559/1.107939/1.902949 s`; pause min/median/P95/max: `0.026109/0.045042/0.060786/0.422990 s`. P95 uses nearest-rank. | Passed. These are host intervals, not acoustic timestamps; pauses are not counted as samples lost inside a valid block. No continuous acquisition is claimed. |
| Start from a clean kernel and explicitly load the overlay | Phase 4.8 ran the installed notebook in a new kernel per session and explicitly programmed the verified BIT/HWH pair. The exact BUILD_ID and artifact hashes are in its run evidence. Step 5.3 also used a new privileged PYNQ notebook kernel and explicit `Overlay(..., download=True)` for the campaign. | Covered by accepted candidate-specific evidence and repeated incidentally in the campaign execution. Do not add a separate retest absent a loader/boot change or concrete failure. |

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

Disposition: instrumentation was prepared locally; step 5.3 executed it on the
board. The pause metric includes host overhead between API calls and is not
treated as lost samples inside a block or as proof of continuous acquisition.

## Step 5.3 physical campaign

One physical campaign completed on board `LaZ2`, with the accepted `0.1.0rc2`
wheel installed in the PYNQ environment and a new privileged notebook kernel.
The notebook verified the approved wheel/BIT/HWH hashes and explicitly loaded
the accepted overlay. No tones or test signals were emitted; the acquisition
used the connected microphones under ambient conditions. No physical mapping,
channel identity, or array geometry was evaluated.

- Campaign ID: `pcm-stability-20261113T184651Z-45725f00`.
- Result: 100/100 blocks passed; zero capture/validation errors, overflows,
  clips, or DMA errors. The final report records `clock_disabled: true`.
- Every saved array reloaded as `(48000,4)` `int32`, 768,000-byte payload, with
  values in signed-24 range and exact frame/word/DMA counters.
- Start/end UTC: `2026-11-13T18:46:51.264764+00:00` /
  `2026-11-13T18:48:42.346159+00:00`.
- Call duration min/median/P95/max: `1.026908/1.046559/1.107939/1.902949 s`.
- Inter-call pause min/median/P95/max: `0.026109/0.045042/0.060786/0.422990 s`
  across 99 intervals. These host pauses do not imply within-block sample loss.
- Block 51's host call took `1.902949 s`; its array, counters and status passed.
  Criterion E sets no upper limit on API call duration, so this is recorded as
  timing variability. It does not establish any loss within that complete
  block; continuous acquisition would need a separate test.
- Continuous acquisition between calls is not claimed.

The complete campaign report, per-block JSON and `.npy` data, executed
notebook, installation log, execution manifest and SHA-256 inventory are under
`evidence/phase-05-step-03/` in the workspace. The continuity handoff records
the campaign result and next phase action.

## Evidence locations

- Phase 4.7 board summary: `evidence/phase-04-step-07/README.md`
- Phase 4.7 board diagnostics: `evidence/phase-04-step-07/run-20261112T233235Z-173042ec/diagnostics-report.json`
- Phase 4.8 physical summary: `evidence/phase-04-step-08/README.md`
- Phase 4.8 block/tone review: `evidence/phase-04-step-08/board-evidence-review.json`
- Phase 4.8 per-block host timestamps: the 60 `*-repNN.json` files below the six `evidence/phase-04-step-08/run-*/pcm-*/` directories
- Phase 4.8 fresh-kernel runner: `evidence/phase-04-step-08/via_jupyter.py`; each run's executed notebook records explicit programming of the candidate
- Hardware timing and warning review: `evidence/phase-04-step-06/review.json`
- Phase 5.3 campaign report and 100 block files:
  `evidence/phase-05-step-03/board/pcm-stability-20261113T184651Z-45725f00/`
- Phase 5.3 execution artifacts:
  `evidence/phase-05-step-03/board/launcher/`
- Phase 5.3 post-transfer checksums: `docs/PHASE5_CAMPAIGN_AUDIT.json` and
  `evidence/phase-05-step-03/board-campaign-audit.json`

## Campaign requirement

The step 5.3 campaign satisfies the 100-block serial-capture criterion and
stores all returned samples and timing/counter records. Inter-call pauses are
not counted as missing samples inside a capture, and these block captures do
not certify continuity between calls. Recovery and clean-kernel evidence remain
covered by accepted phase 4 evidence; no abort, timeout, reset, or standalone
kernel check was repeated in this campaign.

The accepted phase 3 RAW integration lock remains unchanged. Existing phase 4
acceptance and the deferred geometry are not broadened by this matrix.
