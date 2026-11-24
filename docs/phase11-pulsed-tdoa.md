# Phase 11 pulsed-source API

The Phase 11 pulsed candidate adds `BuzzerPulse` for one-shot control of the
PL output on AR2/U13, and `PulsedTdoAAoAEstimator` for a first four-channel
differential-arrival direction estimate. Loading/programming an overlay stays
the caller's responsibility: `BuzzerPulse.from_overlay()` only attaches to an
already loaded overlay.

```python
from pynq_acoustic_camera import (
    AoAConfig, BuzzerPulse, PulseToAConfig, PulsedTdoAAoAEstimator,
)

pulse = BuzzerPulse.from_overlay(overlay)
pulse.configure_ms(5.0)  # reset default is also 5 ms
start_cycle = pulse.fire(wait=True)

estimator = PulsedTdoAAoAEstimator(PulseToAConfig(
    aoa=AoAConfig(
        positions_m=measured_positions_m,
        pcm_channels=mic_to_pcm_columns,
        geometry_provenance="measurement-record-id",
    ),
))
result = estimator.estimate(
    raw_pcm_frames,                 # signed-24 int32 array, original N x 4 order
    sample_rate_hz=48_000,
    frequency_hz=measured_tone_hz,
    baseline_frames=pre_pulse_frame_count,
    first_frame=first_pcm_frame,
)
```

`mic_to_pcm_columns` is a permutation mapping physical Mic1..Mic4 to PCM
columns 0..3. It selects columns inside the calculation only; capture buffers
and saved PCM stay in their original order. `raw_pcm_frames` needs a pre-pulse
baseline followed by the pulse. The frequency should come from the buzzer's
measured acoustic output; the historical 2610 Hz profile is only a starting
reference, not a calibration.

The result includes four per-channel arrival estimates, all six pairwise
`delta_us` values, fitted residuals, and a provisional azimuth/elevation. Its
`TDOA_DIRECTION_CANDIDATE` status does not imply physical calibration or a
precision bound. The fit assumes a plane wave across the full array and the
front hemisphere. It rejects insufficient signal, inconsistent delays, and
degenerate geometry.

TDoA is the initial AoA method because the unknown common trigger-to-sound
latency cancels between microphones. The PL start counter is not currently
locked to the PCM frame counter, so this API does not produce absolute ToA or
source range. Physical positions, orientations, channel identity, frequency,
and environmental conditions still need to be recorded during board
measurements, followed by a held-out known-angle validation.

## Pulse register interface

The PL peripheral runs at 100 MHz with AXI-Lite base address `0x43CA0000`.
`configure_ms()` selects a single pulse width from 10 ns through 1 s;
`fire()` issues one pulse and returns the latched PL start counter. It does not
repeat or alter buzzer supply voltage. The output is a 3.3 V logic signal for
the historical external transistor driver, not a direct buzzer power output.
