# PCM capture (phase 4)

Status: steps 4.1–4.3 complete; standalone PCM filter RTL matches the integer
reference in XSim. The PCM capture API and notebook
`04_pcm_capture.ipynb` are not implemented yet. The frozen filter design and
integer reference are documented in [PCM_FILTER_DESIGN.md](PCM_FILTER_DESIGN.md).

## Baseline and interface

This branch starts at the accepted phase-3 RAW software commit
`3e1c86b52dce0354c49b1136d24850d1aa60d5b6`. Preserve the current RAW API,
loader and candidate provenance. The accepted overlay does not advertise PCM;
software must validate the new candidate identity and PCM capability before use.

The shared contract is `acoustic-camera-capture/1.0`. PCM results will contain
`N` synchronous four-channel frames as a detached `int32` array with shape
`(N,4)` in order mic 0 through mic 3. The signed 24-bit values are sign-extended
to 32 bits. Metadata will identify the format, nominal 48 kHz rate, decimation
50, block counters, clipping count, filter settling and candidate provenance.
Errors or partial DMA blocks must not be returned as valid samples.

The PCM pattern is `16*n+c` for frame `n` and channel `c`; it validates order,
shape, byte count and transport, not the physical microphone or decimator. The
filter's independent fixed-point reference and hardware equivalence belong to
the filter-design and digital-verification steps. The notebook will provide
repeatable capture analysis and preserve each run's report and data.
