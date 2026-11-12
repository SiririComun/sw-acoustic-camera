# RAW capture (phase 3)

`AcousticArray.from_overlay(overlay, expected_build_id, provenance=...)` attaches
to an already loaded and hash-verified RAW BIT/HWH pair. It reads HWH DMA
parameters and IP identity before writing hardware. It never loads an overlay;
the existing XADC and phase-2 loaders remain unchanged. Exclusive control of this
capture IP/DMA is required; do not instantiate PYNQ's automatic DMA driver too.

```python
array.enable_clock()  # bounded CLOCK_READY wait
result = array.capture(n_frames=4096, format='raw_pdm', source='pattern')
array.disable_clock()  # only idle
```

`result.samples` is a detached `(N,) uint32` array, 4*N bytes. For microphones,
bits 0..3 are DATA0..3; for pattern the complete word is 0..N-1. PCM is unavailable.
Metadata records configuration, nominal rate, host UTC execution times, BUILD_ID,
provenance, deadline, DMA bytes and capture counters.

DMA is armed before START; bounded polling checks core/DMA errors, DONE/IDLE,
exact transferred bytes, frame/word counters and overflow. Default timeout is
`max(2, 10*N/2400000)` seconds. Errors return no valid partial block and raise
`RawCaptureError` with diagnostics. Recovery uses ABORT + DMA soft reset + CLEAR;
clock and configuration are preserved. `recover()` exposes the same bounded
sequence for a diagnostic abort. A failed reset prevents reuse and retains any
buffer that DMA could still own; reload a verified overlay before a new session.

Notebook `03_raw_capture.ipynb` defaults to no hardware execution. Candidate
hashes are pinned to the reviewed local step-3.5 pair. Its pattern run prepares five
sizes × ten repetitions with data/metadata evidence. Keep microphones disconnected
until the physical instructions of step 3.6. No notebook cells ran in step 3.4.

Host verification: `python3 tools/verify_raw_capture.py`; this uses fake MMIO and
allocations, not PYNQ. Actual memory transfer and recovery still require 3.5.

The loaded HWH must also describe PS HP0 at 64 bits, matching official PYNQ boot
configuration. DMA and RAW AXI Stream stay 32 bits; SmartConnect converts to HP0.
The board pattern rejected the first HP0=32 candidate. from_overlay now rejects
that mismatch before control writes. Corrected candidate receives a new BUILD_ID.
