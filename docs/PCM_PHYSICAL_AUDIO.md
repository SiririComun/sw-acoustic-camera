# Phase 4.8 — physical four-channel PCM audio

The pinned 4.6 BIT/HWH and exact installed wheel were used with notebook 04
in fresh privileged kernels. Six physical microphone sessions each captured
ten `(48000,4)` `int32` PCM blocks at nominal 48 kHz. Every block reported
48000 frames, 192000 words, 768000 DMA bytes, no clip/overflow/error, and
the clock was disabled after each session. The first block after filter enable
has a large startup transient; it remains in the evidence, while positional
comparisons use the following nine blocks.

The laptop's built-in left speaker replaced the planned external speaker,
with explicit analog-sink routing because the default output was Bluetooth.
Separate 1, 2 and 3 kHz runs passed all 120 channel checks at the nominal
frequency. Minimum peak-to-neighbor-floor margins were 42.17, 52.08 and
73.72 dB against the 10 dB bench threshold. Acoustic distance and output
level were estimated or logged; no external waveform or sound-pressure
measurement was made.

Broadband noise near the first and second physical modules showed the earliest
arrival on logical channels 0 and 1 respectively in 9/9 settled blocks each.
These observations are provisional. The user deferred full physical module
mapping until the microphones are mounted in the square array. The
centered/equidistant six-pair delay test also remains pending because the
current geometry does not provide a controlled center. The complete section-D
physical acceptance and phase-4 closeout remain open. Accepted phase-3 RAW
artifacts and integration lock were preserved.

Full samples, metadata, notebook executions, source logs, hashes and review:
`../evidence/phase-04-step-08/` in the shared workspace. The phase-4 branch
remains local until phase closeout.
