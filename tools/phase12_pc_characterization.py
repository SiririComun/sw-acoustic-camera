"""Repeatable PC-only stress sweep for the Phase 12 two-tone detector.

All inputs are generated PCM. Results describe this synthetic test space only;
they are not acoustic-source detection or physical acceptance evidence.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pynq_acoustic_camera.two_tone import StreamingTwoToneDetector, TwoToneConfig


PACKETS_PER_TRIAL = 4
PACKET_FRAMES = 4096
PHASES = np.asarray((0.0, 0.2, -0.3, 0.45), dtype=np.float64)
SEEDS = (11, 23, 47)


def make_pcm(first_frame, tones, noise_sigma, seed):
    rng = np.random.default_rng(seed + first_frame)
    frames = np.arange(first_frame, first_frame + PACKET_FRAMES)
    values = rng.normal(0.0, noise_sigma, (PACKET_FRAMES, 4))
    for frequency_hz, amplitude in tones:
        values += amplitude * np.cos(
            2 * np.pi * frequency_hz * frames[:, None] / 48000.0
            + PHASES[None, :])
    return np.rint(np.clip(values, -0.999, 0.999) * (1 << 23)).astype(np.int32)


def run_trial(tones, noise_sigma, seed):
    detector = StreamingTwoToneDetector(TwoToneConfig())
    rows = []
    for packet_index in range(PACKETS_PER_TRIAL):
        first_frame = packet_index * PACKET_FRAMES
        rows.extend(detector.process_block(
            make_pcm(first_frame, tones, noise_sigma, seed)))
    active = [source for source in rows[-1]['sources'] if source['present']]
    return {
        'active_sources': [
            {'id': source['id'], 'frequency_hz': source['frequency_hz']}
            for source in active
        ],
        'max_active_sources_any_window': max(
            (sum(source['present'] for source in row['sources']) for row in rows),
            default=0),
    }


def recovered_expected_frequencies(result, expected, tolerance_hz=25.0):
    found = sorted(source['frequency_hz'] for source in result['active_sources'])
    expected = sorted(expected)
    return (len(found) == len(expected)
            and all(abs(actual - target) <= tolerance_hz
                    for actual, target in zip(found, expected)))


def characterize():
    separation_rows = []
    for separation_hz in (60, 80, 100, 120, 150, 200, 400, 800):
        expected = (1550.0, 1550.0 + separation_hz)
        trials = [run_trial(tuple((f, 0.12) for f in expected), 0.001, seed)
                  for seed in SEEDS]
        separation_rows.append({
            'separation_hz': separation_hz,
            'trials': trials,
            'two_frequencies_recovered_trials': sum(
                recovered_expected_frequencies(trial, expected) for trial in trials),
        })

    imbalance_rows = []
    for noise_sigma in (0.001, 0.005, 0.01, 0.02, 0.03):
        for amplitude_ratio in (1, 2, 4, 8, 12):
            expected = (1550.0, 2350.0)
            tones = ((1550.0, 0.20), (2350.0, 0.20 / amplitude_ratio))
            trials = [run_trial(tones, noise_sigma, seed) for seed in SEEDS]
            imbalance_rows.append({
                'noise_sigma_pcm_full_scale': noise_sigma,
                'strong_to_weak_amplitude_ratio': amplitude_ratio,
                'trials': trials,
                'two_frequencies_recovered_trials': sum(
                    recovered_expected_frequencies(trial, expected) for trial in trials),
            })

    negative_rows = []
    for noise_sigma in (0.001, 0.005, 0.01, 0.02, 0.03):
        trials = [run_trial((), noise_sigma, seed) for seed in SEEDS]
        negative_rows.append({
            'case': 'noise_only',
            'noise_sigma_pcm_full_scale': noise_sigma,
            'trials': trials,
            'trials_with_any_active_source': sum(
                trial['max_active_sources_any_window'] > 0 for trial in trials),
        })
    for noise_sigma in (0.001, 0.01, 0.03):
        trials = [run_trial(((1550.0, 0.20),), noise_sigma, seed)
                  for seed in SEEDS]
        negative_rows.append({
            'case': 'one_tone',
            'noise_sigma_pcm_full_scale': noise_sigma,
            'trials': trials,
            'trials_with_more_than_one_active_source': sum(
                trial['max_active_sources_any_window'] > 1 for trial in trials),
        })

    return {
        'scope': 'PC synthetic detector characterization only; not physical acceptance',
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'runtime': {
            'python': sys.version.split()[0],
            'numpy': np.__version__,
            'platform': platform.platform(),
        },
        'detector_config': {
            'sample_rate_hz': 48000,
            'band_hz': [1000.0, 3000.0],
            'window_frames': 2048,
            'hop_frames': 1024,
            'min_separation_hz': 100.0,
            'min_prominence_db': 15.0,
            'min_tone_level_dbfs': -65.0,
            'min_channels': 3,
        },
        'stimulus': {
            'channels': 4,
            'packets_per_trial': PACKETS_PER_TRIAL,
            'frames_per_packet': PACKET_FRAMES,
            'seeds': list(SEEDS),
            'tone_phase_offsets_rad': PHASES.tolist(),
            'quantization': 'signed24 stored in int32',
        },
        'frequency_separation_sweep': separation_rows,
        'amplitude_imbalance_and_noise_sweep': imbalance_rows,
        'negative_control_sweep': negative_rows,
        'interpretation_limits': [
            'Synthetic tones do not establish the count or identity of physical emitters.',
            'White Gaussian noise does not model room reflections, speech, harmonics, or microphone mismatch.',
            'The 25 Hz recovery tolerance is only a reproducible software scoring rule.',
            'The smallest passing sweep point is not a universal or accepted frequency-separation limit.',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        help='optional JSON evidence path; stdout is used by default')
    args = parser.parse_args(argv)
    report = json.dumps(characterize(), indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding='utf-8')
        print(args.output)
    else:
        print(report, end='')


if __name__ == '__main__':
    main()
