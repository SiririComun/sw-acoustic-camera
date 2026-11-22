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
    args = parser.parse_args(argv)
    if not 6 <= args.seconds <= 60:
        raise ValueError('seconds must be between 6 and 60')
    count = round(RATE * args.seconds)
    time_s = np.arange(count, dtype=np.float64) / RATE
    # One second of both tones, then two seconds of silence. Short ramps avoid
    # clicks. The two frequencies remain on separate stereo channels.
    phase_s = np.mod(time_s - 2.0, 3.0)
    envelope = np.clip(phase_s / 0.025, 0.0, 1.0)
    envelope *= np.clip((1.0 - phase_s) / 0.025, 0.0, 1.0)
    envelope *= (time_s >= 2.0) & (time_s < args.seconds - 2.0)
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
    print('{}: {:.3f} s, pulsed 1550 Hz left and 2350 Hz right, '
          '1 s on / 2 s off'.format(args.output, count / RATE))


if __name__ == '__main__':
    main()
