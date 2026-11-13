# Phase 5 closure — PCM acquisition stability

Closed 2026-11-13, America/Bogota. Phase 5 acceptance is limited to robustness
of serial four-channel PCM captures before geometric work.

## Accepted result

The physical campaign `pcm-stability-20261113T184651Z-45725f00` completed
100/100 valid captures of 48,000 frames each on PYNQ-Z2 `LaZ2`, using the
accepted BUILD_ID `B691CC9C9F6249AD938D2063E4767C54`. Each returned array and its
metadata passed exact shape/dtype/payload, signed-24 range, frame/word/DMA
counters, state and provenance checks. There were no capture errors, overflow,
clips or DMA errors; the shared clock was disabled at completion.

The arrays and per-block records are stored in the workspace at
`evidence/phase-05-step-03/board/pcm-stability-20261113T184651Z-45725f00/`.
Post-transfer reload checks and SHA-256 entries for 204 artifacts are in
`docs/PHASE5_CAMPAIGN_AUDIT.json` and
`evidence/phase-05-step-03/board-campaign-audit.json`.

Host call duration min/median/P95/max was
`1.026908/1.046559/1.107939/1.902949 s`; finish-to-next-start pause was
`0.026109/0.045042/0.060786/0.422990 s` over 99 intervals. Block 51 took
1.902949 s and passed its data and counters. These timings record host/API
behavior; the pauses are not counted as lost samples within completed blocks.
The campaign does not establish continuous acquisition between calls.

No tones were emitted. Microphone identity mapping, centered-source geometry,
TDoA, calibration and continuous streaming remain outside phase 5.

## Vivado diagram delivered

The delivered file is
`evidence/phase-05-closeout/phase5-vivado-block-design.pdf`. It is the actual
Vivado 2024.2.2 Block Design export for `pdm_pcm_capture`, copied byte-for-byte
from the accepted phase 4 closeout because the candidate design did not change
during phase 5. Its SHA-256 is
`61e81bf3c304dea847b2d670193daa16492ae97c685eb39ac0de38aa33579078`; the
versioned source BD SHA-256 remains
`40f8ccb6be80e597422272a63e3d48e2352322238da04f6884c71c9f49ccd7fc`.

## Repository disposition

The HW and SW `phase/05-acquisition-stability` branches contain the unchanged
accepted hardware candidate and the phase 5 evidence/closure records. The
phase 3 RAW integration lock is untouched. Geometry remains deferred until the
square array is mounted; passing this campaign does not claim geometry or
continuous-acquisition acceptance.
