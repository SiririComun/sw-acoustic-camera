# Phase 7 closeout — continuous PCM software

Status: **closed, 2026-11-18**. The separate `ContinuousPcmCapture` API and
board runner were used with the ABI 1.1 candidate. Phase 7 passed its synthetic
transport/overload gate and prolonged physical capture on the PYNQ-Z2.

## Physical capture result

The user confirmed four MP34DT01 microphones at Pmod B. The board runner
captured `source=microphones` for 30.0466 s at 48 kHz, four channels,
`pcm_s24_in_s32`. It returned 352 contiguous packets of 4096 frames over five
64-slot ring wraps. STOP drained one additional complete descriptor. Final
counts were 1,445,888 frames, 5,783,552 words and 23,134,208 bytes; overflow and
clip counters were zero; all four returned channels varied. The official Base
overlay was restored and no DMA buffers remained retained. Raw PCM was not
saved; packet hashes, metadata and channel statistics were retained.

The complete board evidence is in the shared workspace at
`../../evidence/phase-07-step-06/physical-pcm-20261118/`. The run used the driver
source available at software commit `ed077826f06b7954e884653dd8618719a8764a8d`;
the reproducible runner was added in commit
`783573e16bbd6ae9289901ea53d7ba9c54cad1eb`.

## Scope and limitations

The routed CDC report retains Critical and Warning classifications; successful
DMA capture does not establish CDC-clean status. Phase 6 remains closed in its
approved scope. Simultaneous PCM and video operation was not tested. The Phase
3 RAW lock, Phase 4 finite PCM candidate and `integration-lock.json` were not
changed.

See the shared-workspace [Phase 7 closeout evidence](../../evidence/phase-07-closeout/README.md)
and [phase handoff](../../context/PHASE7_CLOSURE.md) for the PDF, exact hashes,
and hardware result.
