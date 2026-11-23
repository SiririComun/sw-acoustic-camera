#!/usr/bin/env python3
"""Generate separated, pulsed stereo tones for the Phase 12 technical trial."""

import argparse
from pathlib import Path
import wave

import numpy as np


RATE = 48000


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--seconds', type=float, default=30.0)
    parser.add_argument('--single-frequency', type=float,
                        help='emit one pulsed tone on the left channel only')
    args = parser.parse_args(argv)
    if not 6 <= args.seconds <= 60:
        raise ValueError('seconds must be between 6 and 60')
    if (args.single_frequency is not None
            and not 0 < args.single_frequency < RATE / 2):
        raise ValueError('single-frequency must be between 0 and Nyquist')
    count = round(RATE * args.seconds)
    time_s = np.arange(count, dtype=np.float64) / RATE
    # One second of both tones, then two seconds of silence. Short ramps avoid
    # clicks. The two frequencies remain on separate stereo channels.
    phase_s = np.mod(time_s - 2.0, 3.0)
    envelope = np.clip(phase_s / 0.025, 0.0, 1.0)
    envelope *= np.clip((1.0 - phase_s) / 0.025, 0.0, 1.0)
    envelope *= (time_s >= 2.0) & (time_s < args.seconds - 2.0)
    left_frequency = args.single_frequency or 1550.0
    left = 0.35 * np.sin(2 * np.pi * left_frequency * time_s) * envelope
    right = (np.zeros_like(left) if args.single_frequency is not None else
             0.35 * np.sin(2 * np.pi * 2350.0 * time_s) * envelope)
    stereo = np.column_stack((left, right))
    pcm = np.clip(np.rint(stereo * 32767), -32768, 32767).astype('<i2')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.output), 'wb') as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())
    if args.single_frequency is None:
        description = 'pulsed 1550 Hz left and 2350 Hz right'
    else:
        description = 'pulsed {:.0f} Hz left only'.format(args.single_frequency)
    print('{}: {:.3f} s, {}, 1 s on / 2 s off'.format(
        args.output, count / RATE, description))


if __name__ == '__main__':
    main()
