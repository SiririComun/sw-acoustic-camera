# PCM filter and fixed-point reference

Phase 4.2 freezes the PDM-to-PCM filter specification before RTL. The
machine-readable coefficients and arithmetic are in
[`pynq_acoustic_camera/data/pcm_filter_v1.json`](../pynq_acoustic_camera/data/pcm_filter_v1.json).
The hardware repository carries a byte-identical copy in `docs/`.

## Chain and arithmetic

Four synchronous PDM bits at nominal 2.4 MHz map `0 -> -1`, `1 -> +1`. CIC3
with differential delay 1 and decimation 5 converts to 480 kHz; its gain 125
is absorbed in the downstream scale. Three 32-bit modular integrators and
comb stages produce values bounded by +/-125. A 61-tap symmetric equiripple
FIR consumes every 480 kHz CIC value, then decimates by 10 to 48 kHz. Its
passband is 0–3 kHz, transition is 3–24 kHz, and stopband is 24–240 kHz.

Signed 18-bit coefficients are `round_even(h[k] * 2^(23+5) / 125)`. The
absolute coefficient sum is 2,148,648, so the FIR MAC bound is 268,581,000;
the chosen accumulator is 32-bit signed. Output conversion uses nearest,
ties-to-even rounding after division by 32, signed 24-bit saturation, then
sign extension to `int32`. The filter emits common-phase samples at PDM input
indices 49, 99, 149, ...; output frames 0–5 after reset are invalid, and frame
6 is the first accepted PCM output. The linear-phase delay is 156 PDM periods
(65 us).

Measured on the frozen quantized response: 0.5912 dB combined 0–3 kHz ripple,
at least 74.11 dB rejection for 45–47 kHz images, at least 69.76 dB minimum
sampled rejection from 24 kHz to PDM Nyquist, and DC gain 1.00000016. The
complete analysis and caveats are in the JSON and HW design note. The dense-grid
result is not a formal continuous-frequency proof.

## Reference tools

- `tools/design_pcm_filter.py` regenerates the design manifest using NumPy and
  SciPy at design time. It is not used by the board runtime.
- `tools/pcm_fixed_reference.py` is the standard-library integer oracle for
  later digital comparisons. Its FIR runs on every 480 kHz CIC sample and only
  emits every tenth result; it retains startup validity and clip counts.

The generator was run to create the checked-in JSON. No unit tests, RTL
simulation, synthesis, bitstream, or hardware run were performed in step 4.2.
The next step can implement the RTL against this frozen coefficient table and
compare it to the reference model.
