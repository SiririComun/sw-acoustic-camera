#!/usr/bin/env python3
"""Reproduce the offline PDM-to-PCM filter design manifest.

This tool is a design-time dependency on NumPy/SciPy; the board runtime uses the
frozen integer coefficients in the package data and does not design the filter.
"""

import argparse
import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import scipy
from scipy import signal


FS_PDM_HZ = 2_400_000
CIC_DECIMATION = 5
CIC_ORDER = 3
CIC_GAIN = CIC_DECIMATION ** CIC_ORDER
FS_FIR_HZ = FS_PDM_HZ // CIC_DECIMATION
FIR_DECIMATION = 10
FS_PCM_HZ = FS_FIR_HZ // FIR_DECIMATION
FIR_TAPS = 61
FIR_PASSBAND_HZ = 3_000
FIR_STOPBAND_HZ = FS_PCM_HZ // 2
FIR_COEFF_Q = 5
PCM_FRACTIONAL_BITS = 23
ACCUMULATOR_BITS = 32
INTEGRATOR_BITS = 32
OUTPUT_MIN = -(1 << 23)
OUTPUT_MAX = (1 << 23) - 1
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "pynq_acoustic_camera/data/pcm_filter_v1.json"


def cic_magnitude(frequency_hz):
    """Magnitude of the unity-DC CIC3/R5 response before its downsampler."""
    frequency_hz = np.asarray(frequency_hz, dtype=np.float64)
    return np.abs(
        np.sinc(CIC_DECIMATION * frequency_hz / FS_PDM_HZ)
        / np.sinc(frequency_hz / FS_PDM_HZ)
    ) ** CIC_ORDER


def stage1_alias_frequency(frequency_hz):
    """Fold an input frequency into the 0..240 kHz first-stage Nyquist band."""
    return np.abs(
        (np.asarray(frequency_hz, dtype=np.float64) + FS_FIR_HZ / 2)
        % FS_FIR_HZ
        - FS_FIR_HZ / 2
    )


def db(value):
    return float(20 * np.log10(max(float(value), 1e-300)))


def make_design():
    floating = signal.remez(
        FIR_TAPS,
        [0, FIR_PASSBAND_HZ, FIR_STOPBAND_HZ, FS_FIR_HZ / 2],
        [1, 0],
        weight=[1, 100],
        fs=FS_FIR_HZ,
        maxiter=100,
    )
    floating = floating / np.sum(floating)

    output_scale = 2 ** (PCM_FRACTIONAL_BITS + FIR_COEFF_Q) / CIC_GAIN
    coefficients = np.rint(floating * output_scale).astype(np.int64)
    # Keep the integer table exactly symmetric and its DC sum nearest to unity.
    for index in range(FIR_TAPS // 2):
        coefficients[-1 - index] = coefficients[index]
    target_sum = round(output_scale)
    coefficients[FIR_TAPS // 2] += target_sum - int(np.sum(coefficients))
    quantized_floating = coefficients.astype(np.float64) / output_scale

    response_f, response_h = signal.freqz(
        quantized_floating, worN=262_144, fs=FS_FIR_HZ
    )
    passband = np.abs(response_h[response_f <= FIR_PASSBAND_HZ])
    stopband = np.abs(response_h[response_f >= FIR_STOPBAND_HZ])
    fir_pass_ripple_db = db(passband.max() / passband.min())
    fir_stop_attenuation_db = -db(stopband.max())

    pass_grid = np.linspace(0, FIR_PASSBAND_HZ, 3_001)
    pass_alias = stage1_alias_frequency(pass_grid)
    _, pass_fir = signal.freqz(
        quantized_floating, worN=2 * np.pi * pass_alias / FS_FIR_HZ
    )
    combined_pass = cic_magnitude(pass_grid) * np.abs(pass_fir)
    combined_pass_ripple_db = db(combined_pass.max() / combined_pass.min())

    image_grid = np.linspace(45_000, 47_000, 4_001)
    image_alias = stage1_alias_frequency(image_grid)
    _, image_fir = signal.freqz(
        quantized_floating, worN=2 * np.pi * image_alias / FS_FIR_HZ
    )
    image_attenuation_db = -db(
        np.max(cic_magnitude(image_grid) * np.abs(image_fir))
    )

    useful_grid = np.linspace(1_000, 3_000, 4_001)
    useful_alias = stage1_alias_frequency(useful_grid)
    _, useful_fir = signal.freqz(
        quantized_floating, worN=2 * np.pi * useful_alias / FS_FIR_HZ
    )
    useful_desired = cic_magnitude(useful_grid) * np.abs(useful_fir)
    worst_useful_alias = (0.0, None)
    for fold in range(1, FS_PDM_HZ // (2 * FS_PCM_HZ) + 1):
        for side in (1, -1):
            folded_input = fold * FS_PCM_HZ + side * useful_grid
            valid = (folded_input >= FS_PCM_HZ / 2) & (
                folded_input <= FS_PDM_HZ / 2
            )
            if not np.any(valid):
                continue
            folded_stage1 = stage1_alias_frequency(folded_input[valid])
            _, folded_fir = signal.freqz(
                quantized_floating,
                worN=2 * np.pi * folded_stage1 / FS_FIR_HZ,
            )
            transfer = (
                cic_magnitude(folded_input[valid]) * np.abs(folded_fir)
                / useful_desired[valid]
            )
            peak = int(np.argmax(transfer))
            if transfer[peak] > worst_useful_alias[0]:
                worst_useful_alias = (
                    float(transfer[peak]),
                    {
                        "fold": fold,
                        "side": side,
                        "pcm_frequency_hz": float(useful_grid[valid][peak]),
                        "pdm_frequency_hz": float(folded_input[valid][peak]),
                    },
                )

    # Sample every 1.176 Hz across the complete input stopband through PDM Nyquist.
    high_grid = np.linspace(FS_PCM_HZ / 2, FS_PDM_HZ / 2, 1_000_001)
    high_alias = stage1_alias_frequency(high_grid)
    _, high_fir = signal.freqz(
        quantized_floating, worN=2 * np.pi * high_alias / FS_FIR_HZ
    )
    high_transfer = cic_magnitude(high_grid) * np.abs(high_fir)
    high_peak = int(np.argmax(high_transfer))

    coefficient_error_l1 = float(
        np.sum(np.abs(coefficients - floating * output_scale))
    )
    coefficient_error_bound_lsb = (
        125 * coefficient_error_l1 / (2 ** FIR_COEFF_Q)
    )
    accumulator_bound = int(np.sum(np.abs(coefficients))) * 125
    minimum_accumulator_bits = 2
    while (1 << (minimum_accumulator_bits - 1)) - 1 < accumulator_bound:
        minimum_accumulator_bits += 1
    dc_gain = float(np.sum(coefficients) * CIC_GAIN / 2 ** (23 + FIR_COEFF_Q))
    coefficient_sha256 = hashlib.sha256(
        struct.pack("<{}i".format(FIR_TAPS), *map(int, coefficients))
    ).hexdigest()
    group_delay_pdm_samples = (
        CIC_ORDER * (CIC_DECIMATION - 1) / 2
        + (FIR_TAPS - 1) / 2 * CIC_DECIMATION
    )

    return {
        "schema": "pdm-pcm-filter/1.0",
        "filter_id": "mp34dt01-pdm-2p4m-r5-cic3-fir61-r10-v1",
        "status": "designed_offline_not_yet_implemented",
        "input": {
            "nominal_rate_hz": FS_PDM_HZ,
            "bit_mapping": {"0": -1, "1": 1},
            "channel_count": 4,
            "sampling_event": "one shared sample event for all four PDM bits",
        },
        "stages": [
            {
                "type": "CIC_decimator",
                "order": CIC_ORDER,
                "differential_delay": 1,
                "decimation": CIC_DECIMATION,
                "input_rate_hz": FS_PDM_HZ,
                "output_rate_hz": FS_FIR_HZ,
                "dc_gain": CIC_GAIN,
                "unity_dc_normalization": "included in final FIR coefficient scale",
                "integrator_bits": INTEGRATOR_BITS,
                "comb_bits": INTEGRATOR_BITS,
                "arithmetic": "two_complement_modulo_2^32; no internal rounding",
                "output_bits": 8,
                "output_bound": [-125, 125],
                "decimation_phase": "emit after PDM sample indices 4, 9, 14, ...",
            },
            {
                "type": "linear_phase_equiripple_FIR_decimator",
                "decimation": FIR_DECIMATION,
                "input_rate_hz": FS_FIR_HZ,
                "output_rate_hz": FS_PCM_HZ,
                "taps": FIR_TAPS,
                "passband_hz": [0, FIR_PASSBAND_HZ],
                "transition_band_hz": [FIR_PASSBAND_HZ, FIR_STOPBAND_HZ],
                "stopband_hz": [FIR_STOPBAND_HZ, FS_FIR_HZ // 2],
                "design_method": "SciPy signal.remez, weights [1, 100], then unity-DC normalization",
                "decimation_phase": "emit after CIC frame indices 9, 19, 29, ...",
            },
        ],
        "fixed_point": {
            "pcm_fractional_bits": PCM_FRACTIONAL_BITS,
            "pcm_format": "signed_24_in_signed_32",
            "fir_coefficient_bits": 18,
            "fir_coefficient_fractional_bits": FIR_COEFF_Q,
            "fir_coefficient_formula": "round_even(h[k] * 2^(23+5) / 125)",
            "fir_coefficients": list(map(int, coefficients)),
            "fir_coefficients_symmetric": bool(
                np.array_equal(coefficients, coefficients[::-1])
            ),
            "fir_coefficient_sum": int(np.sum(coefficients)),
            "fir_coefficient_sum_target": target_sum,
            "fir_coefficient_sha256_le_i32": coefficient_sha256,
            "fir_accumulator_bits": ACCUMULATOR_BITS,
            "fir_accumulator_absolute_bound": accumulator_bound,
            "minimum_signed_accumulator_bits_for_bound": minimum_accumulator_bits,
            "output_rounding": "round_to_nearest_ties_to_even_after division by 2^5",
            "output_saturation_range": [OUTPUT_MIN, OUTPUT_MAX],
            "clip_count": "increment once for each saturated channel sample in an accepted block",
            "coefficient_quantization_l1_error": coefficient_error_l1,
            "coefficient_quantization_output_error_bound_lsb": coefficient_error_bound_lsb,
            "coefficient_quantization_output_error_bound_dbfs": db(
                coefficient_error_bound_lsb / (1 << PCM_FRACTIONAL_BITS)
            ),
            "pcm_rounding_error_bound_lsb": 0.5,
        },
        "timing": {
            "total_decimation": CIC_DECIMATION * FIR_DECIMATION,
            "pcm_rate_hz": FS_PCM_HZ,
            "first_pcm_output_pdm_sample_index": 49,
            "pcm_output_phase": "PDM sample indices 49, 99, 149, ...",
            "common_channel_phase": True,
            "filter_settle_frames": 6,
            "settling_rule": "discard PCM output indices 0..5 after reset; output index 6 is the first with 61 full CIC samples, each with complete CIC history",
            "group_delay_pdm_samples": int(group_delay_pdm_samples),
            "group_delay_seconds": group_delay_pdm_samples / FS_PDM_HZ,
            "group_delay_pcm_frames": group_delay_pdm_samples / 50,
        },
        "response": {
            "method": "frequency response of quantized coefficients; CIC response multiplied by FIR response after first-stage frequency folding",
            "numpy_version": np.__version__,
            "scipy_version": scipy.__version__,
            "fir_passband_ripple_0_3khz_db": fir_pass_ripple_db,
            "fir_stopband_minimum_attenuation_24_240khz_db": fir_stop_attenuation_db,
            "combined_passband_ripple_0_3khz_db": combined_pass_ripple_db,
            "combined_dc_gain": dc_gain,
            "combined_dc_gain_error_db": db(dc_gain),
            "combined_attenuation_45_47khz_db": image_attenuation_db,
            "minimum_alias_attenuation_into_1_3khz_db": -db(worst_useful_alias[0]),
            "worst_useful_alias": worst_useful_alias[1],
            "minimum_sampled_attenuation_for_all_input_frequencies_24khz_to_pdm_nyquist_db": -db(
                high_transfer[high_peak]
            ),
            "worst_sampled_highband_input_hz": float(high_grid[high_peak]),
            "highband_grid_points": int(len(high_grid)),
            "highband_grid_spacing_hz": float(high_grid[1] - high_grid[0]),
        },
        "limitations": [
            "Response and alias figures are dense-grid numerical measurements, not a formal continuous-frequency proof.",
            "The exact microphone suffix and modulator noise transfer function are unconfirmed; this budget covers filter arithmetic quantization, not the sensor's shaped PDM quantization noise.",
            "Acoustic sensitivity, clock-pin waveform, and electrical I/O margins are outside this digital design analysis.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    design = make_design()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(design, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("Wrote", args.output)
    print(json.dumps({
        "filter_id": design["filter_id"],
        "coefficient_sha256_le_i32": design["fixed_point"]["fir_coefficient_sha256_le_i32"],
        "combined_passband_ripple_db": design["response"]["combined_passband_ripple_0_3khz_db"],
        "stopband_attenuation_db": design["response"]["fir_stopband_minimum_attenuation_24_240khz_db"],
        "45_47khz_attenuation_db": design["response"]["combined_attenuation_45_47khz_db"],
        "full_alias_minimum_db": design["response"]["minimum_sampled_attenuation_for_all_input_frequencies_24khz_to_pdm_nyquist_db"],
        "filter_settle_frames": design["timing"]["filter_settle_frames"],
    }, indent=2))


if __name__ == "__main__":
    main()
