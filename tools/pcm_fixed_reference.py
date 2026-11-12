"""Integer PDM-to-PCM model for the frozen phase-4 filter design.

The model consumes one simultaneous four-bit PDM frame at a time. It is an
offline oracle for later RTL and board-pattern comparisons, not a runtime
capture driver.
"""

import json
from dataclasses import dataclass
from pathlib import Path


DESIGN_PATH = (
    Path(__file__).resolve().parents[1]
    / "pynq_acoustic_camera/data/pcm_filter_v1.json"
)


def _signed_wrap(value, bits):
    mask = (1 << bits) - 1
    value &= mask
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def round_shift_ties_to_even(value, shift):
    """Round value / 2**shift to nearest integer, with ties to even."""
    if shift < 0:
        raise ValueError("shift must be non-negative")
    if shift == 0:
        return int(value)
    magnitude = abs(int(value))
    denominator = 1 << shift
    quotient, remainder = divmod(magnitude, denominator)
    halfway = denominator >> 1
    if remainder > halfway or (remainder == halfway and quotient & 1):
        quotient += 1
    return -quotient if value < 0 else quotient


@dataclass(frozen=True)
class PcmReferenceFrame:
    """One decimated output vector, including startup validity."""

    index: int
    samples: tuple
    valid: bool
    frame_clip_count: int


class PdmPcmReference:
    """Stateful exact-integer model of the phase-4 CIC3/R5 + FIR61/R10 chain."""

    def __init__(self, design_path=DESIGN_PATH):
        design = json.loads(Path(design_path).read_text(encoding="utf-8"))
        if design.get("filter_id") != "mp34dt01-pdm-2p4m-r5-cic3-fir61-r10-v1":
            raise ValueError("Unsupported PCM filter design")
        fixed = design["fixed_point"]
        stages = design["stages"]
        self.channels = design["input"]["channel_count"]
        self.cic_decimation = stages[0]["decimation"]
        self.cic_order = stages[0]["order"]
        self.cic_gain = stages[0]["dc_gain"]
        self.integrator_bits = stages[0]["integrator_bits"]
        self.fir_decimation = stages[1]["decimation"]
        self.coefficients = tuple(fixed["fir_coefficients"])
        self.coefficient_shift = fixed["fir_coefficient_fractional_bits"]
        self.output_min, self.output_max = fixed["output_saturation_range"]
        self.settle_frames = design["timing"]["filter_settle_frames"]
        self.cic_output_bound = stages[0]["output_bound"][1]
        self.reset()

    def reset(self):
        self._integrators = [[0] * self.cic_order for _ in range(self.channels)]
        self._comb_delays = [[0] * self.cic_order for _ in range(self.channels)]
        self._fir_history = [[0] * len(self.coefficients) for _ in range(self.channels)]
        self._pdm_index = 0
        self._cic_index = 0
        self._pcm_index = 0
        self.clip_count = 0

    @property
    def filter_ready(self):
        return self._pcm_index >= self.settle_frames

    @property
    def pcm_frames_seen(self):
        return self._pcm_index

    def feed(self, pdm_bits):
        """Consume a four-channel 0/1 frame; return a PCM frame every 50 bits.

        The first six PCM outputs are returned with ``valid=False``. The next
        output is the first whose FIR window contains complete post-reset CIC
        history. Invalid startup frames do not contribute to CLIP_COUNT.
        """
        bits = tuple(pdm_bits)
        if len(bits) != self.channels or any(bit not in (0, 1) for bit in bits):
            raise ValueError("pdm_bits must contain four values, each 0 or 1")

        current_integrators = []
        for channel, bit in enumerate(bits):
            value = 1 if bit else -1
            state = self._integrators[channel]
            state[0] = _signed_wrap(state[0] + value, self.integrator_bits)
            for stage in range(1, self.cic_order):
                state[stage] = _signed_wrap(
                    state[stage] + state[stage - 1], self.integrator_bits
                )
            current_integrators.append(state[-1])

        pdm_index = self._pdm_index
        self._pdm_index += 1
        if pdm_index % self.cic_decimation != self.cic_decimation - 1:
            return None

        cic_values = []
        for channel, integrator_value in enumerate(current_integrators):
            delay = self._comb_delays[channel]
            value = integrator_value
            for stage in range(self.cic_order):
                delayed_value = delay[stage]
                delay[stage] = value
                value = _signed_wrap(
                    value - delayed_value, self.integrator_bits
                )
            if not -self.cic_output_bound <= value <= self.cic_output_bound:
                raise ArithmeticError("CIC output exceeded its proven +/-125 bound")
            cic_values.append(value)

        cic_index = self._cic_index
        self._cic_index += 1
        # The FIR runs at 480 kHz. Update its history and MAC on every CIC
        # output; only its result is decimated to 48 kHz below.
        filtered = []
        for channel, value in enumerate(cic_values):
            history = self._fir_history[channel]
            history.insert(0, value)
            del history[len(self.coefficients):]
            accumulator = sum(
                sample * coefficient
                for sample, coefficient in zip(history, self.coefficients)
            )
            rounded = round_shift_ties_to_even(accumulator, self.coefficient_shift)
            saturated = min(self.output_max, max(self.output_min, rounded))
            filtered.append((rounded, saturated))

        if cic_index % self.fir_decimation != self.fir_decimation - 1:
            return None

        frame_index = self._pcm_index
        valid = frame_index >= self.settle_frames
        output = [saturated for _, saturated in filtered]
        frame_clips = sum(rounded != saturated for rounded, saturated in filtered)

        self._pcm_index += 1
        if valid:
            self.clip_count += frame_clips
        return PcmReferenceFrame(
            index=frame_index,
            samples=tuple(output),
            valid=valid,
            frame_clip_count=frame_clips if valid else 0,
        )


def convert_pdm_bits(pdm_frames, design_path=DESIGN_PATH):
    """Convert iterable four-bit frames; retain all output frames and validity."""
    model = PdmPcmReference(design_path)
    outputs = []
    for bits in pdm_frames:
        output = model.feed(bits)
        if output is not None:
            outputs.append(output)
    return outputs, model.clip_count
