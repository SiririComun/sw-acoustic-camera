#!/usr/bin/env python3
"""Export a reproducible PC-only two-source heatmap demo from synthetic PCM."""

import argparse
import json
import math
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pynq_acoustic_camera.continuous_capture import ContinuousPcmPacket
from pynq_acoustic_camera.single_source_aoa import AoAConfig, SingleSourceAoAEstimator
from pynq_acoustic_camera.two_source_pipeline import TwoSourcePcmPipeline
from pynq_acoustic_camera.two_source_visual import TwoSourceVisualState, render_two_source_maps
from pynq_acoustic_camera.two_tone import StreamingTwoToneDetector, TwoToneConfig


SYNTHETIC_SOURCES = ((1550.0, 0.18, 25.0, -12.0),
                     (2350.0, 0.12, -28.0, 15.0))


def synthetic_packet(index, config):
    first = index * 4096
    time = np.arange(first, first + 4096) / 48000.0
    positions = np.asarray(config.positions_m)
    signal = np.zeros((4096, 4), dtype=np.float64)
    for frequency, amplitude, azimuth, elevation in SYNTHETIC_SOURCES:
        azimuth, elevation = map(math.radians, (azimuth, elevation))
        direction = np.array((math.sin(azimuth) * math.cos(elevation),
                              math.sin(elevation),
                              math.cos(azimuth) * math.cos(elevation)))
        delays = positions @ direction / config.speed_of_sound_m_s
        signal += amplitude * np.cos(2 * math.pi * frequency
                                     * (time[:, None] + delays[None, :]))
    samples = np.rint(signal * (1 << 23)).astype(np.int32)
    return ContinuousPcmPacket(samples, {
        'packet_index': index, 'first_frame': first, 'frames': 4096,
        'sample_rate_hz': 48000, 'source': 'microphones',
    })


def test_frame():
    height, width = 480, 640
    y = np.linspace(0, 1, height, dtype=np.float32)[:, None]
    x = np.linspace(0, 1, width, dtype=np.float32)[None, :]
    frame = np.empty((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = np.rint(35 + 55 * x).astype(np.uint8)
    frame[:, :, 1] = np.rint(45 + 40 * y).astype(np.uint8)
    frame[:, :, 2] = np.rint(65 + 35 * x).astype(np.uint8)
    for column in range(80, width, 80):
        cv2.line(frame, (column, 0), (column, height - 1), (75, 75, 75), 1)
    for row in range(80, height, 80):
        cv2.line(frame, (0, row), (width - 1, row), (75, 75, 75), 1)
    cv2.putText(frame, 'PC DEMO / SYNTHETIC PCM', (18, 36),
                cv2.FONT_HERSHEY_SIMPLEX, 0.68, (230, 230, 230), 2)
    return frame


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-sources', type=int, choices=(1, 2), default=2)
    parser.add_argument('--manual-frequencies', type=float, nargs='*', default=())
    args = parser.parse_args(argv)
    detector_config = TwoToneConfig(
        max_sources=args.max_sources,
        manual_frequencies_hz=tuple(args.manual_frequencies))
    aoa_config = AoAConfig(max_refinement_seeds=4)
    pipeline = TwoSourcePcmPipeline(
        detector=StreamingTwoToneDetector(detector_config),
        estimator=SingleSourceAoAEstimator(aoa_config))
    visual = TwoSourceVisualState()
    for index in range(4):
        for row in pipeline.process_packet(synthetic_packet(index, aoa_config)):
            visual.update(row, row['window']['end_frame_exclusive'] / 48000.0)
    snapshots = visual.snapshot(4 * 4096 / 48000.0)
    frame = test_frame()
    rendered = render_two_source_maps(frame, snapshots)
    args.output.mkdir(parents=True, exist_ok=False)
    outputs = {'base': frame, 'combined': rendered['combined_frame']}
    outputs.update({'source_{}'.format(source_id): image for source_id, image
                    in rendered['source_frames'].items()})
    for name, image in outputs.items():
        if not cv2.imwrite(str(args.output / (name + '.png')), image):
            raise RuntimeError('Could not write ' + name + '.png')
    report = {
        'scope': 'PC synthetic visualization only; not physical AoA acceptance',
        'mode': 'manual' if detector_config.manual_frequencies_hz else 'automatic',
        'max_sources': detector_config.max_sources,
        'manual_frequencies_hz': list(detector_config.manual_frequencies_hz),
        'synthetic_sources': [dict(frequency_hz=f, azimuth_deg=az, elevation_deg=el)
                              for f, _, az, el in SYNTHETIC_SOURCES],
        'visible_sources': rendered['visible_sources'],
        'images': sorted(name + '.png' for name in outputs),
        'projection': 'provisional 70x50 degree FOV and reported concentric 50x50 mm array',
    }
    (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n',
                                               encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
