# Phase 4 closeout — PCM software and acoustic response

The user confirmed that phase 4 is scoped to functional capture and audio;
physical geometry is deferred until the microphones are mounted as a square.
Under that scope, the PCM software/API and four-channel physical audio response
are accepted. This does not claim that the original section-D microphone map or
the centered, equidistant six-pair test passed. Both remain explicitly deferred.

## Software and board result

- The exact phase-4 wheel and PCM BIT/HWH pair were verified before use; the
  notebook ran in a new kernel for each physical session.
- Six board sessions supplied 60 blocks × 48,000 frames with exact counters and
  no clips, overflow, or DMA errors. Output shape was `(48000,4)`, dtype `int32`.
- Tones at 1, 2, and 3 kHz were detected in all four channels: 120 channel
  checks, exact nominal FFT peaks, minimum margins over neighboring floor of
  `42.17/52.08/73.72 dB`.
- Broadband tests at two close module positions showed earliest arrival on
  logical channels 0 and 1, respectively, in nine settled blocks per position.
  This is provisional evidence and is not a complete physical channel map.
- Full run data and review: `../evidence/phase-04-step-08/` in the shared
  workspace. The actual Vivado block-design PDF and hashes are in
  `../evidence/phase-04-closeout/`.

The accepted phase-3 RAW candidate and integration lock remain unchanged. Phase
5 stability and later physical geometry work have not started.

The `phase/04-pcm-capture` branch has been published to GitHub. The final
remote commit IDs are recorded in the workspace closeout record. Phase-4
functional acceptance and geometry deferral are documented above.
