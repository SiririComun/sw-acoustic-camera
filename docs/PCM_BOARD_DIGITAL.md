# Phase4.7 — actual-board digital PCM acceptance

The reviewed4.6 candidate (BUILD_ID B691CC9C9F6249AD938D2063E4767C54) was
installed/programmed explicitly on LaZ2/PYNQ-Z2 in a fresh privileged PYNQ kernel.
A separate run copy of the installed notebook04 executed ten code cells; its
packaged defaults remain disabled and its acoustic flag was left false.
Thirty-three installed package files matched the exact4.6 wheel; dependency
versions were unchanged (PYNQ3.1.1, NumPy1.21.5, nbclient0.5.13, nbformat5.4.0).
The installed package came from SWb50502c; runner809d95f adds only the board tool.
No change to FPGA sources or new bitstream was required.

- Restored-wiring RAW check:4096 frames, reserved bits zero, four inputs active.
  Channel transitions:2628/2651/2609/2636. Acoustic identity remains pending.
- PCM pattern:50 complete blocks, sizes1/2/17/4096/65536 ten times each;
  696520 frames,2786080 samples,11144320 bytes. Every sample equals16*n+c;
  shape/dtype, signed24 range, bytes and hardware/DMA counters all correct.
- Arguments and wrong identity rejected before writes (probe writes0).
- Busy configuration write and abort each produced their expected error;
  local recovery preserved configuration/clock/FILTER_READY and the following
  PCM pattern was exact. No partial interrupted block was treated as valid.
- Controlled1us deadline caused timeout; recovery succeeded and next block passed.
- RAW-pattern↔PCM-pattern switching passed. Explicit second programming of the
  same verified pair restored clock/filter readiness and counters to reset;
  enabling/waiting and another PCM pattern passed.
- Final clock disabled. No rewiring and no physical PCM audio acquisition yet.

Downloaded evidence was reviewed locally:111 artifact hashes, ten executed cells,
all arrays compared again with the pattern, RAW activity and metadata/provenance
recomputed. Full record: `evidence/phase-04-step-07/board-evidence-review.json`;
run: `run-20261112T233235Z-173042ec`. Summary: `phase4-step7-verification.json`.

The accepted phase3 RAW pair/lock and previous software loader remain preserved.
This passes the digital board step, not phase4 acoustic acceptance. External
clock frequency/duty/signal integrity remain unmeasured; captures are independent
blocks, not proof of continuity between calls. Next4.8 requires physical PCM
blocks and user-coordinated tones/channel identification/pair-delay checks.
The actual Vivado BD PDF is still due at phase4 closure. Commits remain local.
