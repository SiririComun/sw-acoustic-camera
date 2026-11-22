#!/usr/bin/env python3
"""Phase 12 live PCM, C920 and two-source HDMI maps.

Board trials show two live tonal AoAs and separated map regions, but sustained
dual-tone processing and C920 USB stability remain under investigation.
Geometry, channel identity and camera FOV are provisional.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import traceback

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.phase11_live_camera import bind_video_output, p50_p95
from pynq_acoustic_camera.continuous_capture import ContinuousPcmCapture
from pynq_acoustic_camera.single_source_aoa import AoAConfig, SingleSourceAoAEstimator
from pynq_acoustic_camera.two_source_pipeline import TwoSourcePcmPipeline
from pynq_acoustic_camera.two_source_visual import TwoSourceVisualState, render_two_source_maps
from pynq_acoustic_camera.two_tone import StreamingTwoToneDetector, TwoToneConfig


class LiveState:
    def __init__(self, max_age_s=0.5):
        self.lock = threading.Lock()
        self.visual = TwoSourceVisualState(max_age_s=max_age_s)
        self.error = None
        self.packets = 0
        self.discontinuities = 0
        self.packet_cpu_ms = []
        self.packet_wall_ms = []
        self.packet_period_overruns = 0
        self.aoa_status_counts = Counter()
        self.aoa_status_by_source = Counter()
        self.present_windows_by_source = Counter()
        self.capture_result = None


def capture_worker(overlay, stop_event, state, event_log_path, *,
                   max_sources, manual_frequencies_hz, pcm_channels):
    capture = None
    # Two active tracks roughly double the AoA fit rate. The lighter search
    # reduces load; continuous dual-tone input can still overflow this board.
    config = AoAConfig(pcm_channels=pcm_channels,
                       max_refinement_seeds=2 if max_sources == 2 else 4)
    detector = StreamingTwoToneDetector(TwoToneConfig(
        max_sources=max_sources, manual_frequencies_hz=manual_frequencies_hz))
    pipeline = TwoSourcePcmPipeline(
        detector=detector, estimator=SingleSourceAoAEstimator(config),
        aoa_update_stride=64 if max_sources == 2 else 16,
        max_fits_per_window=1)
    try:
        capture = ContinuousPcmCapture.from_overlay(
            overlay, memory_port='HP2', provenance={
                'phase': 12, 'pcm_memory_port': 'HP2',
                'geometry': 'user-reported 50x50 mm square concentric with camera; provisional',
                'pcm_channels_by_port': list(pcm_channels),
            })
        capture.enable_clock(timeout=5.0)
        capture.wait_filter_ready(timeout=8.0)
        queue = capture.start(source='microphones', frames_per_descriptor=4096,
                              slot_count=64, timeout=5.0)
        with event_log_path.open('a', encoding='utf-8') as events:
            events.write(json.dumps({'type': 'capture_started', 'queue': queue}) + '\n')
            events.flush()
            while not stop_event.is_set():
                packet = capture.read_packet(timeout=1.0)
                wall_start = time.monotonic()
                thread_clock = getattr(time, 'thread_time', time.process_time)
                cpu_start = thread_clock()
                try:
                    rows = pipeline.process_packet(packet)
                except ValueError as error:
                    if 'discontinuity' not in str(error):
                        raise
                    with state.lock:
                        state.discontinuities += 1
                        state.visual.clear()
                    events.write(json.dumps({'type': 'pcm_discontinuity',
                                             'packet_index': packet.metadata['packet_index'],
                                             'message': str(error)}) + '\n')
                    events.flush()
                    continue
                cpu_ms = (thread_clock() - cpu_start) * 1000
                wall_ms = (time.monotonic() - wall_start) * 1000
                period_ms = packet.metadata['frames'] * 1000 / packet.metadata['sample_rate_hz']
                with state.lock:
                    state.packets += 1
                    state.packet_cpu_ms.append(cpu_ms)
                    state.packet_wall_ms.append(wall_ms)
                    state.packet_period_overruns += int(wall_ms > period_ms)
                    for row in rows:
                        state.visual.update(row, time.monotonic())
                        for source in row['sources']:
                            if source['present']:
                                state.present_windows_by_source[source['id']] += 1
                            if source['aoa'] is not None:
                                state.aoa_status_counts[source['aoa']['status']] += 1
                                state.aoa_status_by_source[(source['id'],
                                                            source['aoa']['status'])] += 1
                for row in rows:
                    events.write(json.dumps({'type': 'two_source_window', **row},
                                            allow_nan=False) + '\n')
                events.flush()
        state.capture_result = capture.stop(timeout=5.0).metadata
        capture.disable_clock()
    except BaseException as error:
        with state.lock:
            state.error = '{}: {}'.format(type(error).__name__, error)
            state.visual.clear()
        error_event = {'type': 'capture_error', 'error': state.error,
                       'traceback': traceback.format_exc(),
                       'processed_packets': state.packets,
                       'packet_cpu': p50_p95(state.packet_cpu_ms),
                       'packet_wall': p50_p95(state.packet_wall_ms),
                       'aoa_status_counts': dict(state.aoa_status_counts)}
        if hasattr(error, 'diagnostics'):
            error_event['diagnostics'] = error.diagnostics
        try:
            with event_log_path.open('a', encoding='utf-8') as events:
                events.write(json.dumps(error_event, allow_nan=False) + '\n')
        except OSError:
            pass
        if capture is not None and capture._state == 'running':
            try:
                capture.stop(timeout=5.0)
            except Exception:
                pass


def selected_frame(frame, rendered, view):
    if view == 'combined':
        return rendered['combined_frame']
    source_id = int(view[-1])
    return rendered['source_frames'].get(source_id, frame.copy())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=float, default=30.0)
    parser.add_argument('--max-sources', type=int, choices=(1, 2), default=2)
    parser.add_argument('--manual-frequencies', type=float, nargs='*', default=())
    parser.add_argument('--view', choices=('combined', 'source0', 'source1'),
                        default='combined')
    parser.add_argument('--pcm-channels', type=int, nargs=4, default=(0, 1, 2, 3))
    parser.add_argument('--hfov-deg', type=float, default=70.0)
    parser.add_argument('--vfov-deg', type=float, default=50.0)
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    parser.add_argument('--camera-fps', type=float, default=30.0)
    parser.add_argument('--hdmi-fps', type=float, default=60.0)
    args = parser.parse_args(argv)
    manual = tuple(args.manual_frequencies)
    channels = tuple(args.pcm_channels)
    TwoToneConfig(max_sources=args.max_sources, manual_frequencies_hz=manual)
    AoAConfig(pcm_channels=channels)
    if (not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0
            or not 1 < args.hfov_deg < 170 or not 1 < args.vfov_deg < 170
            or args.width <= 0 or args.height <= 0
            or not math.isfinite(args.camera_fps) or args.camera_fps <= 0
            or not math.isfinite(args.hdmi_fps) or args.hdmi_fps <= 0):
        raise ValueError('invalid duration, field of view, or video mode')
    if args.max_sources == 1 and args.view == 'source1':
        raise ValueError('source1 view requires max_sources=2')
    if os.geteuid() != 0 or sys.prefix != '/usr/local/share/pynq-venv':
        raise RuntimeError('Run as root with the PYNQ venv Python')
    if not args.bit.is_file() or not args.bit.with_suffix('.hwh').is_file():
        raise FileNotFoundError('A matching candidate BIT/HWH pair is required')
    args.output.mkdir(parents=True, exist_ok=False)
    frames_dir = args.output / 'submitted_frames'
    frames_dir.mkdir()
    events_path = args.output / 'events.jsonl'
    bit_sha = hashlib.sha256(args.bit.read_bytes()).hexdigest()
    hwh_sha = hashlib.sha256(args.bit.with_suffix('.hwh').read_bytes()).hexdigest()
    started_utc = datetime.now(timezone.utc).isoformat()

    import phase6_video_perf as video
    video.use_legacy_metadata_parser()
    from pynq import Overlay
    from pynq.lib.video import VideoMode
    from pynq.pl_server.device import Device
    overlay = Overlay(str(args.bit), download=True, device=Device.active_device)
    if not overlay.is_loaded():
        raise RuntimeError('Combined candidate did not load')
    stop_event = threading.Event()
    state = LiveState(max_age_s=0.9 if args.max_sources == 2 else 0.5)
    worker = threading.Thread(
        target=capture_worker, args=(overlay, stop_event, state, events_path),
        kwargs={'max_sources': args.max_sources,
                'manual_frequencies_hz': manual, 'pcm_channels': channels},
        name='pcm-two-aoa', daemon=True)
    worker.start()
    camera = video.cv2.VideoCapture(video.CAMERA_NODE, video.cv2.CAP_V4L2)
    if not camera.isOpened():
        stop_event.set()
        worker.join(timeout=10)
        raise RuntimeError('C920 could not open')
    camera.set(video.cv2.CAP_PROP_FOURCC, video.cv2.VideoWriter_fourcc(*'MJPG'))
    camera.set(video.cv2.CAP_PROP_FRAME_WIDTH, args.width)
    camera.set(video.cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    camera.set(video.cv2.CAP_PROP_FPS, args.camera_fps)

    hdmi = None
    submitted = 0
    visible_counts = Counter()
    camera_read_ms = []
    hdmi_submit_ms = []
    exported = []
    exported_visibility_counts = set()
    invalid_frames = 0
    start_time = time.monotonic()
    try:
        hdmi = bind_video_output(overlay)
        hdmi.cacheable_frames = False
        hdmi.configure(VideoMode(args.width, args.height, 24, args.hdmi_fps))
        hdmi.start()
        print('PHASE12_LIVE_READY bit_sha256={} view={}'.format(bit_sha, args.view),
              flush=True)
        while time.monotonic() - start_time < args.duration_seconds:
            if state.error:
                raise RuntimeError('PCM/AoA worker failed: ' + state.error)
            read_start = time.monotonic()
            ok, frame = camera.read()
            read_ms = (time.monotonic() - read_start) * 1000
            camera_read_ms.append(read_ms)
            if read_ms > 2000:
                raise RuntimeError('C920 frame read stalled for {:.1f} ms'.format(read_ms))
            if not ok or frame is None or frame.shape[:2] != (args.height, args.width):
                invalid_frames += 1
                if invalid_frames >= 5:
                    raise RuntimeError('C920 returned five missing or malformed frames')
                continue
            invalid_frames = 0
            with state.lock:
                snapshots = state.visual.snapshot(time.monotonic())
            rendered = render_two_source_maps(
                frame, snapshots, hfov_deg=args.hfov_deg, vfov_deg=args.vfov_deg,
                include_individual=args.view != 'combined', include_maps=False)
            visible = rendered['visible_sources']
            visible_counts[len(visible)] += 1
            selected = selected_frame(frame, rendered, args.view)
            out = hdmi.newframe()
            np.asarray(out)[...] = selected
            if (submitted < 5 or
                    (len(visible) not in exported_visibility_counts and len(exported) < 20) or
                    (submitted % max(1, int(args.camera_fps)) == 0
                     and len(exported) < 20)):
                path = frames_dir / 'submitted_{:06d}.png'.format(submitted)
                if not video.cv2.imwrite(str(path), np.asarray(out)):
                    raise RuntimeError('Could not export PS framebuffer before HDMI')
                exported.append({'file': path.name, 'frame_index': submitted,
                                 'view': args.view, 'visible_sources': visible})
                exported_visibility_counts.add(len(visible))
            submit_start = time.monotonic()
            hdmi.writeframe(out)
            hdmi_submit_ms.append((time.monotonic() - submit_start) * 1000)
            submitted += 1
    finally:
        stop_event.set()
        worker.join(timeout=12)
        camera.release()
        if hdmi is not None:
            hdmi.close()
    if worker.is_alive() or state.error:
        raise RuntimeError('PCM/AoA worker did not stop cleanly: ' + str(state.error))
    with state.lock:
        report = {
            'status': 'board_run_recorded_not_physically_calibrated',
            'started_utc': started_utc,
            'candidate': {'bit': str(args.bit), 'bit_sha256': bit_sha,
                          'hwh_sha256': hwh_sha},
            'provisional_setup': {
                'array_geometry': 'user-reported concentric 50x50 mm square',
                'pcm_channels_by_port': list(channels), 'pcm_memory_port': 'HP2',
                'camera_hfov_deg': args.hfov_deg, 'camera_vfov_deg': args.vfov_deg,
                'max_sources': args.max_sources,
                'manual_frequencies_hz': list(manual), 'view': args.view,
                'aoa_update_stride_windows': 64 if args.max_sources == 2 else 16,
                'aoa_max_fits_per_window': 1,
                'aoa_max_refinement_seeds': 2 if args.max_sources == 2 else 4,
                'visual_max_age_s': 0.9 if args.max_sources == 2 else 0.5,
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
            'camera_hdmi': {
                'frames_submitted': submitted,
                'fps_submitted': submitted / max(1e-9, time.monotonic() - start_time),
                'visible_source_count_by_frame': dict(visible_counts),
                'camera_read': p50_p95(camera_read_ms),
                'hdmi_submit': p50_p95(hdmi_submit_ms),
                'ps_frame_exports': exported,
                'frame_export_point': 'PS framebuffer immediately before HDMI writeframe',
            },
            'physical_calibration': 'pending Phases 9 and 10.4',
        }
    (args.output / 'result.json').write_text(json.dumps(report, indent=2,
                                                allow_nan=False) + '\n', encoding='utf-8')
    print('PHASE12_LIVE_DONE frames={} packets={}'.format(submitted, state.packets),
          flush=True)


if __name__ == '__main__':
    main()
