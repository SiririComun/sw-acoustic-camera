#!/usr/bin/env python3
"""Generate a bounded stereo stimulus for the Phase 12 technical trial."""

import argparse
from pathlib import Path
import wave

import numpy as np


RATE = 48000


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--seconds', type=float, default=30.0)
    args = parser.parse_args(argv)
    if not 6 <= args.seconds <= 60:
        raise ValueError('seconds must be between 6 and 60')
    count = round(RATE * args.seconds)
    time_s = np.arange(count, dtype=np.float64) / RATE
    envelope = np.clip((time_s - 2.0) / 0.025, 0.0, 1.0)
    envelope *= np.clip((args.seconds - 2.0 - time_s) / 0.025, 0.0, 1.0)
    left = 0.35 * np.sin(2 * np.pi * 1550.0 * time_s) * envelope
    right = 0.35 * np.sin(2 * np.pi * 2350.0 * time_s) * envelope
    stereo = np.column_stack((left, right))
    pcm = np.clip(np.rint(stereo * 32767), -32768, 32767).astype('<i2')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.output), 'wb') as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())
    print('{}: {:.3f} s, 1550 Hz left and 2350 Hz right, simultaneous'.format(
        args.output, count / RATE))


if __name__ == '__main__':
    main()
