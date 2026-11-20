#!/usr/bin/env python3
"""Create repeatable phase 8.10 calibration/acceptance WAV manifests.

This utility only writes files. It never plays audio or accesses the board.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import wave

import numpy as np


RATE_HZ = 48000
REFERENCE_PEAK = 0.12


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tone-hz', type=int, required=True,
                        choices=(900, 1200, 1877, 2800, 3100))
    parser.add_argument('--levels-db', type=int, nargs='+', default=[-30])
    parser.add_argument('--repetitions-per-level', type=int, default=20)
    parser.add_argument('--lead-seconds', type=float, default=0.25)
    parser.add_argument('--active-seconds', type=float, default=1.5)
    parser.add_argument('--tail-seconds', type=float, default=0.25)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--contrast-target-db', type=float, default=6.0)
    args = parser.parse_args()
    if args.repetitions_per_level < 1:
        raise ValueError('repetitions-per-level must be positive')
    if args.lead_seconds < 0.1 or args.active_seconds < 1.0 or args.tail_seconds < 0.1:
        raise ValueError('Require >=0.1 s lead/tail and >=1 s active tone')
    if any(level > 0 or level < -48 for level in args.levels_db):
        raise ValueError('WAV levels must be from -48 through 0 dB relative')

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    lead_frames = round(args.lead_seconds * RATE_HZ)
    active_frames = round(args.active_seconds * RATE_HZ)
    tail_frames = round(args.tail_seconds * RATE_HZ)
    active_time = np.arange(active_frames, dtype=np.float64) / RATE_HZ
    ramp_frames = min(round(0.02 * RATE_HZ), active_frames // 4)
    envelope = np.ones(active_frames, dtype=np.float64)
    envelope[:ramp_frames] = np.linspace(0.0, 1.0, ramp_frames, endpoint=False)
    envelope[-ramp_frames:] = np.linspace(1.0, 0.0, ramp_frames, endpoint=False)
    files = {}
    entries = []
    for level_db in sorted(set(args.levels_db)):
        peak = REFERENCE_PEAK * 10 ** (level_db / 20)
        samples = np.concatenate((
            np.zeros(lead_frames, dtype=np.float64),
            peak * np.sin(2 * np.pi * args.tone_hz * active_time) * envelope,
            np.zeros(tail_frames, dtype=np.float64),
        ))
        pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767).astype('<i2')
        filename = 'tone_{:04d}hz_level_{:+03d}db.wav'.format(args.tone_hz, level_db)
        path = output / filename
        with wave.open(str(path), 'wb') as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(RATE_HZ)
            target.writeframes(pcm.tobytes())
        file_record = {
            'file': filename,
            'tone_hz': args.tone_hz,
            'relative_level_db': level_db,
            'peak_wav_full_scale': peak,
            'sample_rate_hz': RATE_HZ,
            'lead_seconds': lead_frames / RATE_HZ,
            'active_seconds': active_frames / RATE_HZ,
            'tail_seconds': tail_frames / RATE_HZ,
            'frames': len(pcm),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        files[level_db] = file_record
        for repetition in range(1, args.repetitions_per_level + 1):
            entries.append({**file_record, 'repetition': repetition})

    random.Random(args.seed).shuffle(entries)
    manifest = {
        'phase_step': '8.10',
        'purpose': 'measured_contrast_calibration_or_independent_acceptance',
        'sample_rate_hz': RATE_HZ,
        'format': 'mono_pcm_s16le',
        'tone_hz': args.tone_hz,
        'analysis_band_hz': [1000, 3000],
        'reference_peak_full_scale': REFERENCE_PEAK,
        'operating_contrast_target_db': args.contrast_target_db,
        'contrast_definition': 'measured approximate PCM energy in +/-50 Hz around target versus remainder of 1-3 kHz; not calibrated acoustic SNR',
        'warning': 'WAV relative amplitude is not received PCM contrast; calibrate from the PYNQ trace.',
        'repetitions_per_level': args.repetitions_per_level,
        'random_seed': args.seed,
        'randomized_entries': entries,
        'files': [files[key] for key in sorted(files)],
    }
    (output / 'manifest.json').write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print('Created {} WAV level(s), {} playback entries; no audio played'.format(
        len(files), len(entries)))


if __name__ == '__main__':
    main()
