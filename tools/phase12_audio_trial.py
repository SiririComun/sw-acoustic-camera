#!/usr/bin/env python3
"""Run Phase 12 dual-tone PCM/AoA on LaZ2 without camera or HDMI."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.phase11_live_camera import p50_p95
from tools.phase12_live_camera import LiveState, capture_worker
from pynq_acoustic_camera.single_source_aoa import AoAConfig
from pynq_acoustic_camera.two_tone import TwoToneConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=float, default=45.0)
    parser.add_argument('--max-sources', type=int, choices=(1, 2), default=2)
    parser.add_argument('--manual-frequencies', type=float, nargs='*', default=())
    parser.add_argument('--pcm-channels', type=int, nargs=4, default=(0, 1, 2, 3))
    args = parser.parse_args(argv)
    manual = tuple(args.manual_frequencies)
    channels = tuple(args.pcm_channels)
    TwoToneConfig(max_sources=args.max_sources, manual_frequencies_hz=manual)
    AoAConfig(pcm_channels=channels)
    if not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0:
        raise ValueError('duration must be finite and positive')
    if os.geteuid() != 0 or sys.prefix != '/usr/local/share/pynq-venv':
        raise RuntimeError('Run as root with the PYNQ venv Python')
    if not args.bit.is_file() or not args.bit.with_suffix('.hwh').is_file():
        raise FileNotFoundError('A matching candidate BIT/HWH pair is required')

    args.output.mkdir(parents=True, exist_ok=False)
    event_path = args.output / 'events.jsonl'
    bit_sha = hashlib.sha256(args.bit.read_bytes()).hexdigest()
    hwh_sha = hashlib.sha256(args.bit.with_suffix('.hwh').read_bytes()).hexdigest()
    started_utc = datetime.now(timezone.utc).isoformat()

    from pynq import Overlay
    from pynq.pl_server.device import Device
    overlay = Overlay(str(args.bit), download=True, device=Device.active_device)
    if not overlay.is_loaded():
        raise RuntimeError('Combined candidate did not load')

    stop_event = threading.Event()
    state = LiveState(max_age_s=0.9 if args.max_sources == 2 else 0.5)
    worker = threading.Thread(
        target=capture_worker, args=(overlay, stop_event, state, event_path),
        kwargs={'max_sources': args.max_sources,
                'manual_frequencies_hz': manual, 'pcm_channels': channels},
        name='pcm-two-aoa', daemon=True)
    worker.start()
    try:
        ready_deadline = time.monotonic() + 12.0
        while not state.capture_ready.wait(0.1):
            if state.error:
                raise RuntimeError('PCM/AoA worker failed: ' + state.error)
            if time.monotonic() >= ready_deadline:
                raise RuntimeError('PCM capture did not become ready within 12 s')
        print('PHASE12_AUDIO_READY bit_sha256={} sources={}'.format(
            bit_sha, args.max_sources), flush=True)
        run_start = time.monotonic()
        while time.monotonic() - run_start < args.duration_seconds:
            if state.error:
                raise RuntimeError('PCM/AoA worker failed: ' + state.error)
            time.sleep(0.1)
    finally:
        stop_event.set()
        worker.join(timeout=12.0)
    if worker.is_alive():
        raise RuntimeError('PCM/AoA worker did not stop within 12 s')
    if state.error:
        raise RuntimeError('PCM/AoA worker failed: ' + state.error)

    with state.lock:
        report = {
            'status': 'audio_only_board_run_not_physically_calibrated',
            'started_utc': started_utc,
            'candidate': {'bit': str(args.bit), 'bit_sha256': bit_sha,
                          'hwh_sha256': hwh_sha},
            'provisional_setup': {
                'array_geometry': 'user-reported concentric 50x50 mm square',
                'pcm_channels_by_port': list(channels),
                'pcm_memory_port': 'HP2', 'max_sources': args.max_sources,
                'manual_frequencies_hz': list(manual),
                'aoa_update_stride_windows': 64 if args.max_sources == 2 else 16,
                'aoa_max_fits_per_window': 1,
                'aoa_max_refinement_seeds': 2 if args.max_sources == 2 else 4,
            },
            'capture': state.capture_result,
            'pcm_packets': state.packets,
            'pcm_discontinuities': state.discontinuities,
            'pcm_packets_over_nominal_processing_period': state.packet_period_overruns,
            'pcm_packet_cpu': p50_p95(state.packet_cpu_ms),
            'pcm_packet_wall': p50_p95(state.packet_wall_ms),
            'aoa_status_counts': dict(state.aoa_status_counts),
            'aoa_status_by_source': {
                '{}:{}'.format(source_id, status): count
                for (source_id, status), count in state.aoa_status_by_source.items()},
            'present_windows_by_source': dict(state.present_windows_by_source),
            'video': 'not used; camera and HDMI left outside this trial',
            'physical_calibration': 'pending Phases 9 and 10.4',
        }
    (args.output / 'result.json').write_text(
        json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('PHASE12_AUDIO_DONE packets={} aoa={}'.format(
        state.packets, dict(state.aoa_status_by_source)), flush=True)


if __name__ == '__main__':
    main()
