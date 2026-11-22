"""Bounded PC benchmark of synthetic PCM packets through detector and AoA."""
import argparse
import json
import math
from pathlib import Path
import statistics
import time

import numpy as np

from pynq_acoustic_camera.continuous_capture import ContinuousPcmPacket
from pynq_acoustic_camera.single_source_aoa import AoAConfig
from pynq_acoustic_camera.single_source_pipeline import SingleSourcePcmPipeline


def percentile(values, fraction):
    ordered = sorted(values)
    index = fraction * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packets', type=int, default=120)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.packets < 3:
        parser.error('--packets must be at least three')

    rate, frames, frequency = 48000, 4096, 1877.0
    config = AoAConfig()
    direction = np.array((math.sin(math.radians(25))*math.cos(math.radians(-12)),
                          math.sin(math.radians(-12)),
                          math.cos(math.radians(25))*math.cos(math.radians(-12))))
    phases = np.asarray(config.positions_m) @ direction / config.speed_of_sound_m_s
    pipeline = SingleSourcePcmPipeline()
    cpu_ms, wall_ms, estimates = [], [], []
    statuses = {}
    window_count = 0
    for index in range(args.packets):
        first = index * frames
        t = np.arange(first, first + frames) / rate
        signal = 0.2 * np.cos(2*math.pi*frequency*(t[:, None] + phases[None, :]))
        samples = np.rint(signal * (1 << 23)).astype(np.int32)
        packet = ContinuousPcmPacket(samples, {
            'source': 'microphones', 'packet_index': index,
            'first_frame': first, 'frames': frames, 'sample_rate_hz': rate,
        })
        cpu_start, wall_start = time.process_time_ns(), time.perf_counter_ns()
        rows = pipeline.process_packet(packet)
        cpu_ms.append((time.process_time_ns() - cpu_start) / 1e6)
        wall_ms.append((time.perf_counter_ns() - wall_start) / 1e6)
        window_count += len(rows)
        for row in rows:
            aoa = row['aoa']
            if aoa is not None:
                statuses[aoa['status']] = statuses.get(aoa['status'], 0) + 1
                if aoa['direction'] is not None:
                    estimates.append((aoa['azimuth_deg'], aoa['elevation_deg']))
    period_ms = 1000 * frames / rate
    result = {
        'source': 'deterministic synthetic four-channel plane wave on PC',
        'sample_rate_hz': rate, 'frequency_hz': frequency,
        'packets': args.packets, 'frames_per_packet': frames,
        'packet_period_ms_nominal': period_ms,
        'windows_emitted': window_count, 'aoa_status_counts': statuses,
        'valid_aoa_windows': len(estimates),
        'median_azimuth_deg': statistics.median(x for x, _ in estimates) if estimates else None,
        'median_elevation_deg': statistics.median(y for _, y in estimates) if estimates else None,
        'pipeline_cpu_ms_p50': percentile(cpu_ms, 0.5),
        'pipeline_cpu_ms_p95': percentile(cpu_ms, 0.95),
        'pipeline_cpu_ms_max': max(cpu_ms),
        'pipeline_wall_ms_p95': percentile(wall_ms, 0.95),
        'packets_cpu_over_nominal_period': sum(value > period_ms for value in cpu_ms),
        'scope_note': 'Host synthetic processing only; excludes board capture, I/O and physical AoA calibration.',
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
