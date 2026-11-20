#!/usr/bin/env python3
"""Characterize the phase-8 tone prototype with reproducible synthetic PCM.

This is development evidence. No detection thresholds are accepted by running
it, and PC timing is not a substitute for timing on the PYNQ-Z2.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pynq_acoustic_camera.single_tone import StreamingSingleToneDetector, ToneDetectorConfig  # noqa: E402


RATE = 48000
PACKET_FRAMES = 4096
GAINS = np.array([1.0, 0.9, 0.8, 0.7])
SEEDS = (19, 37, 61, 83, 107)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values, p):
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=np.float64), p))


def summarize(values):
    return {
        'count': len(values),
        'p50': percentile(values, 50),
        'p95': percentile(values, 95),
        'max': max(values) if values else None,
    }


def band_noise(rng, frames, *, colored):
    """Unit-RMS, independent channel noise confined to the selected 1–3 kHz band."""
    raw = rng.normal(size=(frames, 4))
    frequencies = np.fft.rfftfreq(frames, 1.0 / RATE)
    weight = ((frequencies >= 1000.0) & (frequencies <= 3000.0)).astype(np.float64)
    if colored:
        weight *= np.sqrt(1000.0 / np.maximum(frequencies, 1000.0))
    spectrum = np.fft.rfft(raw, axis=0) * weight[:, None]
    filtered = np.fft.irfft(spectrum, n=frames, axis=0)
    filtered -= filtered.mean(axis=0, keepdims=True)
    filtered /= np.sqrt(np.mean(filtered * filtered, axis=0, keepdims=True))
    return filtered


def make_pcm(case, seed):
    rng = np.random.default_rng(seed)
    frames = case['packets'] * PACKET_FRAMES
    noise = band_noise(rng, frames, colored=case['colored_noise'])
    tone_hz = case['tone_hz']
    if tone_hz is None:
        audio = noise * (0.03 * GAINS)
    else:
        amplitude = 0.04 if case['in_band'] else 0.08
        snr_db = case['snr_db']
        noise_rms = amplitude / math.sqrt(2.0) / (10.0 ** (snr_db / 20.0))
        audio = noise * (noise_rms * GAINS)
        sample_indices = np.arange(frames)
        active = ((sample_indices >= case['onset_frame'])
                  & (sample_indices < case['offset_frame']))
        phase = np.array([0.0, 0.4, 0.8, 1.2])
        tone = amplitude * np.sin(
            2.0 * np.pi * tone_hz * sample_indices[:, None] / RATE + phase)
        audio += tone * active[:, None] * GAINS
    if np.max(np.abs(audio)) >= 0.95:
        raise RuntimeError('Synthetic scenario clipped before PCM quantization')
    return np.rint(audio * (1 << 23)).astype(np.int32)


def run_case(case, seed, config):
    pcm = make_pcm(case, seed)
    detector = StreamingSingleToneDetector(config)
    observations = []
    cpu_ms = []
    wall_ms = []
    expected_end = config.window_frames
    expected_window = 0
    for first in range(0, len(pcm), PACKET_FRAMES):
        block = pcm[first:first + PACKET_FRAMES]
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        output = detector.process_block(block)
        cpu_ms.append((time.process_time_ns() - cpu_start) / 1e6)
        wall_ms.append((time.perf_counter_ns() - wall_start) / 1e6)
        if len(output) > math.ceil(len(block) / config.hop_frames):
            raise RuntimeError('Detector emitted an unbounded number of windows')
        for item in output:
            if item['window_index'] != expected_window or item['end_frame_exclusive'] != expected_end:
                raise RuntimeError('Detector window sequence is discontinuous')
            expected_window += 1
            expected_end += config.hop_frames
            observations.append((first + len(block), item))

    positives = 0
    eligible = 0
    frequency_errors = []
    first_detected = [None] * 4
    false_positives = 0
    negative_windows = 0
    for delivered_at_frame, item in observations:
        fully_active = (case['in_band'] and item['first_frame'] >= case['onset_frame']
                        and item['end_frame_exclusive'] <= case['offset_frame'])
        clear_negative = (not case['in_band'] or
                          item['end_frame_exclusive'] <= case['onset_frame'] or
                          item['first_frame'] >= case['offset_frame'] + config.window_frames)
        for channel in item['channels']:
            index = channel['channel']
            if fully_active:
                eligible += 1
                if channel['present']:
                    positives += 1
                    frequency_errors.append(abs(channel['estimated_frequency_hz'] - case['tone_hz']))
                    if first_detected[index] is None:
                        first_detected[index] = delivered_at_frame
            if clear_negative:
                negative_windows += 1
                false_positives += int(channel['present'])

    latencies = []
    if case['in_band']:
        latencies = [1000.0 * (frame - case['onset_frame']) / RATE
                     for frame in first_detected if frame is not None]
    return {
        'seed': seed,
        'eligible_positive_windows': eligible,
        'detected_positive_windows': positives,
        'negative_windows': negative_windows,
        'false_positive_windows': false_positives,
        'first_detection_latency_ms_by_channel': latencies,
        'channels_detected': len(latencies),
        'frequency_abs_error_hz': summarize(frequency_errors),
        'cpu_ms_per_packet': summarize(cpu_ms),
        'wall_ms_per_packet': summarize(wall_ms),
        'window_count': len(observations),
    }


def case_definitions():
    onset = int(0.25 * RATE)
    offset = int(1.50 * RATE)
    cases = [
        dict(name='noise_only_white', tone_hz=None, snr_db=None,
             in_band=False, colored_noise=False, packets=60),
        dict(name='noise_only_colored', tone_hz=None, snr_db=None,
             in_band=False, colored_noise=True, packets=60),
    ]
    for frequency in (1200.0, 1877.0, 2800.0):
        for snr_db in (-6.0, 0.0, 6.0):
            cases.append(dict(name='tone_{}_snr_{:+g}'.format(int(frequency), snr_db),
                              tone_hz=frequency, snr_db=snr_db, in_band=True,
                              colored_noise=False, packets=24))
    for frequency in (500.0, 950.0, 3050.0, 3500.0):
        cases.append(dict(name='outside_{}_snr_+20'.format(int(frequency)),
                          tone_hz=frequency, snr_db=20.0, in_band=False,
                          colored_noise=False, packets=24))
    for case in cases:
        case['onset_frame'] = onset
        case['offset_frame'] = offset
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--window-frames', type=int, default=2048)
    parser.add_argument('--hop-frames', type=int)
    parser.add_argument('--min-prominence-db', type=float, default=15.0)
    args = parser.parse_args()
    config = ToneDetectorConfig(
        window_frames=args.window_frames,
        hop_frames=(args.hop_frames if args.hop_frames is not None
                    else args.window_frames // 2),
        min_prominence_db=args.min_prominence_db,
    )
    report = {
        'phase_step': '8.3-development-characterization',
        'status': 'characterization_only_no_accepted_detection_criteria',
        'data_source': 'deterministic_synthetic_pcm_generated_on_PC',
        'raw_pcm_saved': False,
        'sample_rate_hz': RATE,
        'packet_frames': PACKET_FRAMES,
        'snr_definition': 'tone RMS divided by integrated 1-3 kHz noise RMS before signed24 quantization',
        'latency_definition': 'end of containing 4096-frame packet minus synthetic onset; ignores board transport and scheduling',
        'cpu_timing_scope': 'PC detector process_block only, excludes PCM capture and board runtime',
        'config': asdict(config),
        'seeds': list(SEEDS),
        'detector_sha256': sha256(ROOT / 'pynq_acoustic_camera/single_tone.py'),
        'runner_sha256': sha256(Path(__file__)),
        'cases': [],
    }
    for case in case_definitions():
        runs = [run_case(case, seed, config) for seed in SEEDS]
        positive = sum(run['detected_positive_windows'] for run in runs)
        eligible = sum(run['eligible_positive_windows'] for run in runs)
        false = sum(run['false_positive_windows'] for run in runs)
        negative = sum(run['negative_windows'] for run in runs)
        report['cases'].append({
            'scenario': case,
            'run_count': len(runs),
            'detected_channels': sum(run['channels_detected'] for run in runs),
            'total_in_band_channels': len(SEEDS) * 4 if case['in_band'] else 0,
            'positive_window_detection_rate': positive / eligible if eligible else None,
            'false_positive_window_rate': false / negative if negative else None,
            'first_detection_latency_ms': summarize([
                value for run in runs for value in run['first_detection_latency_ms_by_channel']]),
            'frequency_error_p95_hz_across_runs': percentile([
                run['frequency_abs_error_hz']['p95'] for run in runs
                if run['frequency_abs_error_hz']['p95'] is not None], 95),
            'pc_cpu_ms_p95_across_runs': percentile([
                run['cpu_ms_per_packet']['p95'] for run in runs], 95),
            'runs': runs,
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print('PHASE8_SYNTHETIC_CHARACTERIZATION_WRITTEN=' + str(args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
