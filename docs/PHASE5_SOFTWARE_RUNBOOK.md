# Phase 5.2 — PCM stability campaign software

## Scope

This software prepares one serial campaign of 100 physical microphone captures.
Each call requests 48,000 frames of four-channel `pcm_s24_in_s32` at nominal
48 kHz. The accepted phase-4 candidate is pinned by BUILD_ID and BIT/HWH
SHA-256. The runner is not a substitute for the physical campaign: step 5.2 is
local preparation, and no board result is implied by these files.

The runner enables the shared clock and waits for the PCM filter once, before
block 1. For every returned capture it preserves the NumPy data and a JSON record
before continuing. It checks the sample shape, dtype, bytes, signed-24 range,
candidate provenance, frame/word/DMA counters, status bits, clips, overflows and
DMA errors. Before enabling the hardware it requires free evidence storage for
the 100 sample arrays plus a 16 MiB reserve. The first capture exception or
failed check ends the campaign. It does not retry, inject faults, or repeat the
phase-4 abort/timeout/reset checks.
The clock is disabled in cleanup and that result is recorded.

## Timing interpretation

Each attempted call has UTC and monotonic host start/end times. The report
includes call-duration statistics and the finish-to-next-start interval for
each adjacent pair, with P95 calculated by nearest rank. Startup/readiness time
is recorded separately and is not part of the first capture call.

The pause is a host-side interval between API calls; it may include local
evidence writes and Python overhead. It is not an acoustic timestamp or a lost
sample count. Passing 100 separate, complete blocks does not establish
continuous sampling between those calls.

## Board notebook

Use `pynq_acoustic_camera/data/notebooks/05_acquisition_stability.ipynb` after
installing package version `0.1.0rc2` in the authorized board kernel. Its
`RUN_CAMPAIGN` flag defaults to `False`. When enabled, it verifies the pinned
candidate hashes, explicitly programs the candidate, attaches the checked PCM
API with provenance and writes a unique evidence directory under
`/home/xilinx/acoustic_camera_phase5/evidence`.

The campaign report is `campaign-report.json`; `block-NNN.json` records contain
metadata and validation findings, and each returned `block-NNN.npy` contains
the complete array. A failed call has a metadata record and diagnostics but no
sample file because the API does not return partial arrays. A caught interrupt
is marked `interrupted`; an abrupt process termination can leave the last report
marked `running`. Neither state is a passing 100-block run.
