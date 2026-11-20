#!/usr/bin/env python3
"""Create reproducible mono WAV stimuli for the phase-8 physical pilot.

This prepares files only. It never plays audio or accesses the PYNQ.
The electrical WAV ratio is not the SNR received by a microphone.
"""
import argparse
import hashlib
import json
from pathlib import Path
import wave

import numpy as np


RATE = 48000
SEED = 20261119
FREQUENCIES_HZ = (1200, 1877, 2800)
LEAD_SECONDS = 1
ACTIVE_SECONDS = 6
TAIL_SECONDS = 1


def write_wav(path, signal):
    pcm = np.rint(np.clip(signal, -1.0, 1.0) * 32767.0).astype('<i2')
    with wave.open(str(path), 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(RATE)
        target.writeframes(pcm.tobytes())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise FileExistsError('Output directory must be empty to preserve the stimulus manifest')

    count = ACTIVE_SECONDS * RATE
    rng = np.random.default_rng(SEED)
    frequencies = np.fft.rfftfreq(count, 1.0 / RATE)
    spectrum = np.fft.rfft(rng.standard_normal(count))
    spectrum[(frequencies < 1000) | (frequencies > 3000)] = 0
    noise = np.fft.irfft(spectrum, n=count)
    noise /= np.sqrt(np.mean(noise * noise))
    time_axis = np.arange(count) / RATE
    ramp = min(int(0.02 * RATE), count // 2)
    envelope = np.ones(count)
    envelope[:ramp] = np.linspace(0.0, 1.0, ramp, endpoint=False)
    envelope[-ramp:] = np.linspace(1.0, 0.0, ramp, endpoint=False)
    lead = np.zeros(LEAD_SECONDS * RATE)
    tail = np.zeros(TAIL_SECONDS * RATE)
    files = []

    cases = [('noise_only', 0.0, 0.06 * noise)]
    for tone_hz in FREQUENCIES_HZ:
        tone = np.sin(2 * np.pi * tone_hz * time_axis)
        cases.append(('tone_{}_only'.format(tone_hz), tone_hz, 0.12 * tone))
        cases.append(('tone_{}_plus_noise'.format(tone_hz), tone_hz,
                      0.12 * tone + 0.06 * noise))
    for name, tone_hz, active in cases:
        samples = np.concatenate((lead, active * envelope, tail))
        path = args.output / (name + '.wav')
        files.append({'file': path.name, 'sha256': write_wav(path, samples),
                      'tone_hz': tone_hz, 'frames': len(samples)})

    manifest = {
        'purpose': 'phase_8_exploratory_physical_pilot_only',
        'sample_rate_hz': RATE,
        'format': 'mono_pcm_s16le',
        'seed': SEED,
        'lead_seconds': LEAD_SECONDS,
        'active_seconds': ACTIVE_SECONDS,
        'tail_seconds': TAIL_SECONDS,
        'nominal_wav_tone_rms_to_noise_rms_db': 20 * np.log10((0.12 / np.sqrt(2)) / 0.06),
        'warning': 'Speaker volume, room and microphone response alter received SNR; measure it from PCM.',
        'files': files,
    }
    (args.output / 'manifest.json').write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print('Created {} WAV files; no audio was played'.format(len(files)))


if __name__ == '__main__':
    main()
