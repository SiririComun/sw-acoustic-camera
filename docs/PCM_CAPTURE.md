# Four-channel RAW/PCM API and notebook (phase 4.5)

`pynq_acoustic_camera.AcousticArray` now supports the phase-4 PCM-capable interface
(0xF) and delegates accepted phase-3 RAW hardware (0xB) to the preserved RAW
implementation. The accepted RAW loader/candidate and notebook03 remain intact.
Construction is read-only. `from_overlay` attaches to an already loaded and
hash-verified BIT/HWH pair, validates HWH DMA settings/HP0 and hardware identity,
and never programs the FPGA. A PCM pair does not exist yet; it will be built in4.6.

```python
from pynq_acoustic_camera import AcousticArray, CaptureError

array = AcousticArray.from_overlay(verified_loaded_overlay, expected_build_id,
                                   provenance=candidate_provenance)
array.enable_clock()
try:
    result = array.capture(48000, format='pcm_s24_in_s32', source='microphones')
    audio = result.samples.astype('float64') / 2**23
finally:
    array.disable_clock()
```

PCM returns a detached `(N,4) int32` array, channel order0,1,2,3, with signed24
values sign-extended to32 bits. DMA receives exactly16*N bytes; N=1..65536.
RAW returns the existing `(N,) uint32` representation and4*N bytes. The new
hardware accepts both formats; old RAW hardware rejects PCM before writes.
`CaptureResult` and `CaptureError` alias the existing result/error types for
backward-compatible callers.

## Timing, validation and recovery

Physical PCM waits read-only for FILTER_READY after CLOCK_READY. An explicit
`wait_filter_ready(timeout=...)` is also available. Pattern capture requires only
CLOCK_READY and bypasses the filter. The driver does not reset the running filter
or remove another six samples per block; hardware already handles settling.

The capture wait defaults to `max(2 s,10*N/fs_nominal)`:2 seconds for the largest
RAW block and13.6533 seconds for65536 PCM frames. Readiness, DMA arming and local
recovery have their own finite waits. DMA is armed before START. Success requires
DONE/idle, exact byte/frame/word counts, zero overflow/errors, plausible CLIP_COUNT
and valid signed24 extension. RAW/pattern clips must be zero. Hardware clips count
channel samples, not frames. Each failure returns diagnostics and no partial
valid block. The stream is aborted/reset/cleared; clock, configuration and filter
history remain active. If reset cannot establish safety, the allocation is
retained and the object refuses reuse until a verified overlay is restored.

Metadata identifies source/format, shape/bytes, nominal rate, decimation,
filter ID/coefficient hash, settling/readiness, clip/capture/DMA counters,
BUILD_ID, provenance and host UTC execution times. Host times do not measure the
acoustic sample clock. Source pattern marks `filter_applied=False`.

## Analysis and notebook04

`pcm_analysis.analyze_pcm` reports DC, AC RMS/dBFS, endpoint sample counts and
Hann spectral measurements. Its tone bench check uses a dominant peak in0.5–5kHz,
within±2% of the nominal1/2/3kHz tone and at least10dB above median neighbor bin
power, excluding±100Hz around the peak. This is not calibrated SPL or instrument
SNR. Endpoint counts do not identify hardware saturation events by themselves.

`estimate_pair_delays` reports all six channel pairs using1–3kHz normalized
correlation with an integer lag (default±16 samples). Positive means the second
channel follows the first. Use broadband signals, repeat, and record geometry;
pure tones can be ambiguous and a boundary lag needs review.

Packaged `04_pcm_capture.ipynb` defaults to all hardware flags false and has no
outputs. It attaches to an already loaded pair after hash verification and has
**no FPGA download call**. BIT/HWH hashes remain `None` until4.6. Prepared runs:
brief RAW activity check of restored wiring, five pattern sizes×10, ten physical
48000-frame blocks, separate tone sessions, channel identity and six-pair delays.
Every enabled session preserves samples/metadata and an error report, then
attempts clock shutdown. Existing installed notebook edits are not overwritten.

## Local evidence

`python tools/verify_pcm_capture.py`:17 checks passed for API/models, analysis,
notebook defaults and resource installation. `python tools/verify_raw_capture.py`:
all7 existing checks passed. The wheel was built and its PCM modules, filter JSON
and notebook bytes matched the sources; no package was installed. Host models
cannot verify actual DMA-to-DDR transfer, microphone timing or acoustic quality.
See [phase4-step5-verification.json](phase4-step5-verification.json) for hashes
and evidence paths, [PCM_HARDWARE_INTERFACE.md](PCM_HARDWARE_INTERFACE.md) for the
source interface, and the hardware note for integration results. Next4.6 builds
and pins the actual candidate before board use.
