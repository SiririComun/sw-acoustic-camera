#!/usr/bin/env python3
"""Run continuous four-channel AoA beside live C920 -> HDMI OUT video.

PCM capture and angle estimation run on a worker thread. The PS composites a
red circle onto each current camera frame using only the latest valid AoA
result, then submits that frame to the Base HDMI output. Exported PNGs are
copied from the same PS framebuffer immediately before HDMI submission.

Geometry, channel identity and camera FOV are provisional. A red marker proves
that this software path produced and submitted a frame; it is not physical AoA
acceptance or a claim that the angle is calibrated.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import traceback

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pynq_acoustic_camera.continuous_capture import ContinuousPcmCapture
from pynq_acoustic_camera.single_source_aoa import AoAConfig, SingleSourceAoAEstimator
from pynq_acoustic_camera.single_source_pipeline import SingleSourcePcmPipeline


class LiveState:
    def __init__(self):
        self.lock = threading.Lock()
        self.direction = None
        self.direction_time = None
        self.last_status = 'WAITING_FOR_TONE'
        self.error = None
        self.capture = None
        self.packets = 0
        self.packet_cpu_ms = []
        self.packet_wall_ms = []
        self.packet_period_overruns = 0
        self.discontinuities = 0
        self.status_counts = Counter()
        self.valid_windows = 0
        self.last_packet_index = None
        self.capture_result = None


def percentile(values, fraction):
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=np.float64), fraction * 100.0))


def direction_pixel(direction, width, height, hfov_deg, vfov_deg):
    """Project the provisional array-facing AoA convention into camera pixels."""
    x = width * (0.5 - math.tan(math.radians(direction['azimuth_deg']))
                 / (2.0 * math.tan(math.radians(hfov_deg / 2.0))))
    y = height * (0.5 - math.tan(math.radians(direction['elevation_deg']))
                  / (2.0 * math.tan(math.radians(vfov_deg / 2.0))))
    return int(round(x)), int(round(y))


def capture_worker(overlay, stop_event, state, event_log_path):
    capture = None
    # The Zynq-7000 CPU cannot run the full 16-seed search for every 21 ms
    # overlap window. Retain a useful provisional fit with fewer seeds and
    # refresh it at roughly 3 Hz while the tone remains active.
    estimator = SingleSourceAoAEstimator(AoAConfig(max_refinement_seeds=4))
    pipeline = SingleSourcePcmPipeline(estimator=estimator, aoa_update_stride=16)
    try:
        capture = ContinuousPcmCapture.from_overlay(
            overlay, memory_port='HP2', provenance={
                'phase': 11,
                'pcm_memory_port': 'HP2',
                'geometry': 'reported 50x50 mm square, concentric with camera; provisional',
                'pcm_channels_by_port': [0, 1, 2, 3],
            })
        with state.lock:
            state.capture = capture
        capture.enable_clock(timeout=5.0)
        capture.wait_filter_ready(timeout=8.0)
        start_info = capture.start(source='microphones',
                                   frames_per_descriptor=4096,
                                   slot_count=64,
                                   timeout=5.0)
        with event_log_path.open('a', encoding='utf-8') as events:
            events.write(json.dumps({'type': 'capture_started', 'utc': datetime.now(
                timezone.utc).isoformat(), 'queue': start_info}) + '\n')
            events.flush()
            while not stop_event.is_set():
                packet = capture.read_packet(timeout=1.0)
                packet_wall_start = time.monotonic()
                thread_clock = getattr(time, 'thread_time', time.process_time)
                packet_cpu_start = thread_clock()
                try:
                    rows = pipeline.process_packet(packet)
                except ValueError as error:
                    if 'discontinuity' not in str(error):
                        raise
                    with state.lock:
                        state.discontinuities += 1
                        state.direction = None
                        state.direction_time = None
                        state.last_status = 'PCM_DISCONTINUITY'
                    events.write(json.dumps({'type': 'pcm_discontinuity',
                                             'packet_index': packet.metadata['packet_index'],
                                             'message': str(error)}) + '\n')
                    events.flush()
                    continue
                cpu_ms = (thread_clock() - packet_cpu_start) * 1000.0
                wall_ms = (time.monotonic() - packet_wall_start) * 1000.0
                packet_period_ms = packet.metadata['frames'] * 1000.0 / packet.metadata['sample_rate_hz']
                with state.lock:
                    state.packets += 1
                    state.packet_cpu_ms.append(cpu_ms)
                    state.packet_wall_ms.append(wall_ms)
                    state.packet_period_overruns += int(wall_ms > packet_period_ms)
                    state.last_packet_index = packet.metadata['packet_index']
                for row in rows:
                    aoa = row['aoa']
                    status = (aoa['status'] if aoa is not None else
                              'AOA_UPDATE_SKIPPED' if row['aoa_skipped'] else
                              'NO_ACTIVE_SYSTEM_TONE')
                    with state.lock:
                        state.status_counts[status] += 1
                        if aoa is not None and status == 'VALID_PROVISIONAL':
                            state.last_status = status
                            state.direction = {
                                'azimuth_deg': aoa['azimuth_deg'],
                                'elevation_deg': aoa['elevation_deg'],
                                'frequency_hz': aoa['frequency_hz'],
                                'phase_fit_rms_rad': aoa['phase_fit_rms_rad'],
                                'first_frame': aoa['first_frame'],
                                'window_index': aoa['window_index'],
                                'status': status,
                                'geometry_provenance': aoa['geometry_provenance'],
                                'pcm_channels_by_port': aoa['pcm_channels_by_port'],
                            }
                            state.direction_time = time.monotonic()
                            state.valid_windows += 1
                        elif row['aoa_skipped']:
                            state.last_status = ('VALID_PROVISIONAL'
                                                 if state.direction is not None
                                                 else 'AOA_UPDATE_SKIPPED')
                        else:
                            state.last_status = status
                            state.direction = None
                            state.direction_time = None
                    events.write(json.dumps({'type': 'aoa_window',
                                             'packet_index': row['packet_index'],
                                             'window': row['window'], 'aoa': aoa,
                                             'aoa_skipped': row['aoa_skipped']},
                                            allow_nan=False) + '\n')
                events.flush()
        state.capture_result = capture.stop(timeout=5.0).metadata
        capture.disable_clock()
    except BaseException as error:
        with state.lock:
            state.error = '{}: {}'.format(type(error).__name__, error)
            state.direction = None
            state.direction_time = None
        error_event = {'type': 'capture_error', 'error': state.error,
                       'traceback': traceback.format_exc()}
        if hasattr(error, 'diagnostics'):
            error_event['diagnostics'] = error.diagnostics
        with state.lock:
            error_event['processed_packets'] = state.packets
            error_event['packet_cpu_ms'] = p50_p95(state.packet_cpu_ms)
            error_event['packet_wall_ms'] = p50_p95(state.packet_wall_ms)
            error_event['aoa_valid_windows'] = state.valid_windows
            error_event['status_counts'] = dict(state.status_counts)
        try:
            with event_log_path.open('a', encoding='utf-8') as events:
                events.write(json.dumps(error_event, allow_nan=False) + '\n')
        except OSError:
            pass
        if capture is not None:
            try:
                if capture._state == 'running':
                    capture.stop(timeout=5.0)
            except Exception:
                pass


def p50_p95(values):
    return {'count': len(values), 'p50_ms': percentile(values, 0.50),
            'p95_ms': percentile(values, 0.95),
            'max_ms': float(max(values)) if values else None}


def bind_video_output(overlay):
    """Bind the retained HDMI OUT hierarchy to its VDMA in the minimal BD.

    PYNQ's HDMIWrapper performs this automatically only when both HDMI IN and
    HDMI OUT pipelines exist. Phase 11 intentionally prunes HDMI IN, so the
    generic ``video`` hierarchy leaves ``VideoOut._vdma`` unset.
    """
    video = overlay.video
    hdmi = video.hdmi_out
    vdma = video.axi_vdma
    if hdmi._vdma is None:
        hdmi._vdma = vdma
    if hdmi._vdma is not vdma:
        raise RuntimeError('HDMI OUT is not bound to the retained VDMA')
    return hdmi


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=float, default=60.0)
    parser.add_argument('--hfov-deg', type=float, default=70.0)
    parser.add_argument('--vfov-deg', type=float, default=50.0)
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    parser.add_argument('--camera-fps', type=float, default=30.0)
    parser.add_argument('--hdmi-fps', type=float, default=60.0)
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.prefix != '/usr/local/share/pynq-venv':
        raise RuntimeError('Run as root with the PYNQ venv Python')
    if not args.bit.is_file() or not args.bit.with_suffix('.hwh').is_file():
        raise FileNotFoundError('A matching candidate BIT/HWH pair is required')
    if (not math.isfinite(args.duration_seconds) or args.duration_seconds <= 0
            or not 1 < args.hfov_deg < 170 or not 1 < args.vfov_deg < 170):
        raise ValueError('Duration and provisional camera FOV are invalid')
    if args.width <= 0 or args.height <= 0 or args.camera_fps <= 0 or args.hdmi_fps <= 0:
        raise ValueError('Video dimensions and rates must be positive')
    args.output.mkdir(parents=True, exist_ok=False)
    frame_dir = args.output / 'submitted_frames'
    frame_dir.mkdir()
    run_started_utc = datetime.now(timezone.utc).isoformat()
    event_log_path = args.output / 'events.jsonl'
    stop_event = threading.Event()
    state = LiveState()
    bit_sha256 = hashlib.sha256(args.bit.read_bytes()).hexdigest()
    hwh_sha256 = hashlib.sha256(args.bit.with_suffix('.hwh').read_bytes()).hexdigest()

    # Reuse the tested PYNQ 3.1 metadata parser workaround; this loads the
    # supplied combined candidate, never the stock Base bitstream.
    import phase6_video_perf as video
    video.use_legacy_metadata_parser()
    from pynq import Overlay
    from pynq.lib.video import VideoMode
    from pynq.pl_server.device import Device
    overlay = Overlay(str(args.bit), download=True, device=Device.active_device)
    if not overlay.is_loaded():
        raise RuntimeError('Combined candidate did not load')

    worker = threading.Thread(target=capture_worker,
                              args=(overlay, stop_event, state, event_log_path),
                              name='pcm-aoa', daemon=True)
    worker.start()
    camera = video.cv2.VideoCapture(video.CAMERA_NODE, video.cv2.CAP_V4L2)
    if not camera.isOpened():
        stop_event.set()
        worker.join(timeout=10.0)
        raise RuntimeError('C920 could not open')
    camera.set(video.cv2.CAP_PROP_FOURCC,
               video.cv2.VideoWriter_fourcc(*'MJPG'))
    camera.set(video.cv2.CAP_PROP_FRAME_WIDTH, args.width)
    camera.set(video.cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    camera.set(video.cv2.CAP_PROP_FPS, args.camera_fps)

    hdmi = None
    submitted = 0
    camera_read_ms = []
    hdmi_submit_ms = []
    exported = []
    marker_frames = 0
    no_direction_frames = 0
    invalid_camera_frames = 0
    start_time = time.monotonic()
    try:
        hdmi = bind_video_output(overlay)
        hdmi.cacheable_frames = False
        hdmi.configure(VideoMode(args.width, args.height, 24, args.hdmi_fps))
        hdmi.start()
        print('PHASE11_LIVE_READY bit_sha256={} pcm_port=HP2'.format(bit_sha256), flush=True)
        while time.monotonic() - start_time < args.duration_seconds:
            if state.error:
                raise RuntimeError('PCM/AoA worker failed: ' + state.error)
            read_start = time.monotonic()
            ok, frame = camera.read()
            camera_read_ms.append((time.monotonic() - read_start) * 1000.0)
            if not ok or frame is None or frame.shape[:2] != (args.height, args.width):
                invalid_camera_frames += 1
                if invalid_camera_frames >= 5:
                    raise RuntimeError('C920 returned five missing or malformed frames')
                continue
            invalid_camera_frames = 0
            with state.lock:
                direction = (dict(state.direction) if state.direction is not None
                             and state.direction_time is not None
                             and time.monotonic() - state.direction_time <= 0.5 else None)
                direction_age_ms = ((time.monotonic() - state.direction_time) * 1000.0
                                    if direction is not None else None)
                last_status = state.last_status
            if direction is not None:
                pixel = direction_pixel(direction, args.width, args.height,
                                        args.hfov_deg, args.vfov_deg)
                if 0 <= pixel[0] < args.width and 0 <= pixel[1] < args.height:
                    video.cv2.circle(frame, pixel, 20, (0, 0, 255), -1)
                    marker_frames += 1
                else:
                    pixel = None
            else:
                pixel = None
                no_direction_frames += 1
            out = hdmi.newframe()
            np.asarray(out)[...] = frame
            if submitted < 5 or (submitted % max(1, int(args.camera_fps)) == 0
                                 and len(exported) < 20):
                path = frame_dir / 'submitted_{:06d}.png'.format(submitted)
                if not video.cv2.imwrite(str(path), np.asarray(out)):
                    raise RuntimeError('Could not export PS framebuffer before HDMI')
                exported.append({'file': path.name, 'frame_index': submitted,
                                 'marker_pixel': list(pixel) if pixel else None,
                                 'aoa_status': last_status,
                                 'aoa_age_ms': direction_age_ms})
            submit_start = time.monotonic()
            hdmi.writeframe(out)
            hdmi_submit_ms.append((time.monotonic() - submit_start) * 1000.0)
            submitted += 1
    finally:
        stop_event.set()
        worker.join(timeout=12.0)
        camera.release()
        if hdmi is not None:
            hdmi.close()

    if worker.is_alive():
        raise RuntimeError('PCM/AoA worker did not stop within 12 seconds')
    if state.error:
        raise RuntimeError('PCM/AoA worker failed: ' + state.error)
    with state.lock:
        report = {
            'status': 'measurement_recorded_not_evaluated',
            'started_utc': run_started_utc,
            'candidate': {'bit': str(args.bit), 'bit_sha256': bit_sha256,
                          'hwh_sha256': hwh_sha256},
            'provisional_setup': {
                'array_geometry': 'reported 50x50 mm square, concentric with camera',
                'pcm_channels_by_port': [0, 1, 2, 3],
                'pcm_memory_port': 'HP2',
                'camera_hfov_deg': args.hfov_deg,
                'camera_vfov_deg': args.vfov_deg,
            },
            'capture': state.capture_result,
            'pcm_packets': state.packets,
            'pcm_discontinuities': state.discontinuities,
            'pcm_packets_over_nominal_processing_period': state.packet_period_overruns,
            'pcm_packet_cpu': p50_p95(state.packet_cpu_ms),
            'pcm_packet_wall': p50_p95(state.packet_wall_ms),
            'aoa_status_counts': dict(state.status_counts),
            'aoa_valid_windows': state.valid_windows,
            'last_aoa_status': state.last_status,
            'camera_hdmi': {
                'frames_submitted': submitted,
                'frames_with_valid_aoa_marker': marker_frames,
                'frames_without_current_valid_aoa': no_direction_frames,
                'fps_submitted': submitted / max(1e-9, time.monotonic() - start_time),
                'camera_read': p50_p95(camera_read_ms),
                'hdmi_submit': p50_p95(hdmi_submit_ms),
                'ps_frame_exports': exported,
                'frame_export_point': 'PS framebuffer immediately before HDMI writeframe',
            },
            'physical_calibration': 'pending Phase 9; this is not physical AoA acceptance',
        }
    (args.output / 'result.json').write_text(json.dumps(report, indent=2,
                                                        allow_nan=False) + '\n',
                                               encoding='utf-8')
    print('PHASE11_LIVE_DONE frames={} marked={} packets={} aoa_valid={}'.format(
        submitted, marker_frames, report['pcm_packets'], state.valid_windows), flush=True)


if __name__ == '__main__':
    main()
