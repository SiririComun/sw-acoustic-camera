#!/usr/bin/env python3
"""Create a physical tone-level sweep for phase 8.7; never play audio."""
import argparse
import hashlib
import json
from pathlib import Path
import wave

import numpy as np


RATE_HZ = 48000
TONE_HZ = 1877
REFERENCE_PEAK = 0.12
LEVELS_DB = (0, -6, -12, -18, -24, -30, -36)
LEAD_SECONDS = 1
ACTIVE_SECONDS = 6
TAIL_SECONDS = 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)

    count = ACTIVE_SECONDS * RATE_HZ
    t = np.arange(count) / RATE_HZ
    ramp = int(0.02 * RATE_HZ)
    envelope = np.ones(count)
    envelope[:ramp] = np.linspace(0, 1, ramp, endpoint=False)
    envelope[-ramp:] = np.linspace(1, 0, ramp, endpoint=False)
    sine = np.sin(2 * np.pi * TONE_HZ * t) * envelope
    files = []
    for level_db in LEVELS_DB:
        peak = REFERENCE_PEAK * 10 ** (level_db / 20)
        samples = np.concatenate((np.zeros(LEAD_SECONDS * RATE_HZ),
                                  peak * sine,
                                  np.zeros(TAIL_SECONDS * RATE_HZ)))
        pcm = np.rint(samples * 32767).astype('<i2')
        path = args.output / ('tone_1877_level_{:+03d}db.wav'.format(level_db))
        with wave.open(str(path), 'wb') as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(RATE_HZ)
            target.writeframes(pcm.tobytes())
        files.append({'file': path.name, 'relative_level_db': level_db,
                      'peak_wav_full_scale': peak,
                      'frames': len(samples),
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

    manifest = {
        'phase_step': '8.7',
        'purpose': 'physical_tone_to_real_office_ambience_contrast_curve',
        'sample_rate_hz': RATE_HZ,
        'format': 'mono_pcm_s16le',
        'tone_hz': TONE_HZ,
        'reference': 'Same 1877 Hz, 0.12-peak waveform as phase 8.5 tone-only WAV.',
        'levels_db_relative_to_reference': list(LEVELS_DB),
        'lead_seconds': LEAD_SECONDS,
        'active_seconds': ACTIVE_SECONDS,
        'tail_seconds': TAIL_SECONDS,
        'warning': 'Relative WAV level is not the received acoustic contrast; measure it from PCM.',
        'files': files,
    }
    (args.output / 'manifest.json').write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print('Created {} tone WAVs; no audio was played'.format(len(files)))


if __name__ == '__main__':
    main()
