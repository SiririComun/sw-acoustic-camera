#!/usr/bin/env python3
"""Prepare a bounded live PCM/detector measurement on the PYNQ-Z2.

Run only in phase 8.5 after confirming microphone connections and arranging
the physical stimulus. This is exploratory evidence, not acceptance.
"""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import traceback

import numpy as np

import phase8_pcm_baseline_board as base


def packet_spectrum(samples, tone_hz, f_min_hz, f_max_hz):
    """Per-channel in-band RMS and optional approximate tone/noise split."""
    values = samples.astype(np.float64) / (1 << 23)
    values -= values.mean(axis=0, keepdims=True)
    window = np.hanning(len(values))
    spectrum = np.fft.rfft(values * window[:, None], axis=0)
    frequency = np.fft.rfftfreq(len(values), 1.0 / base.PCM_RATE_HZ)
    power = np.abs(spectrum) ** 2
    power[1:-1] *= 2.0
    power /= len(values) * np.sum(window * window)
    band = (frequency >= f_min_hz) & (frequency <= f_max_hz)
    band_rms = np.sqrt(np.sum(power[band], axis=0))
    result = {'band_rms_dbfs': [20 * math.log10(max(float(x), 1e-15)) for x in band_rms]}
    if tone_hz is not None:
        tone_bins = band & (np.abs(frequency - tone_hz) <= 50.0)
        noise_bins = band & ~tone_bins
        tone_power = np.sum(power[tone_bins], axis=0)
        noise_power = np.sum(power[noise_bins], axis=0)
        result['approx_tone_to_other_band_db'] = [
            10 * math.log10(max(float(t), 1e-30) / max(float(n), 1e-30))
            for t, n in zip(tone_power, noise_power)]
        result['tone_band_rms_dbfs'] = [
            10 * math.log10(max(float(x), 1e-30)) for x in tone_power]
    return result


def tone_track(samples, tone_hz):
    """Short matched-frequency energy trace for post-run onset estimation."""
    if tone_hz is None:
        return None
    stride = 256
    indices = np.arange(stride)
    reference = np.exp(-2j * np.pi * tone_hz * indices / base.PCM_RATE_HZ)
    window = np.hanning(stride)
    reference *= window
    chunks = samples.reshape(-1, stride, 4).astype(np.float64) / (1 << 23)
    chunks -= chunks.mean(axis=1, keepdims=True)
    amplitude = 2 * np.abs(np.einsum('nsc,s->nc', chunks, reference)) / window.sum()
    return [[20 * math.log10(max(float(value) / math.sqrt(2), 1e-15))
             for value in row] for row in amplitude]


def measure(capture_class, detector_class, config_class, overlay, args, report, status_path):
    capture = capture_class.from_overlay(overlay, provenance={
        'phase_step': '8.5_exploratory', 'source': 'physical_microphones',
        'candidate_build_id': base.BUILD_ID,
    })
    if capture.clock.identity.build_id != base.BUILD_ID or capture.clock.identity.channels != 4:
        raise RuntimeError('Unexpected continuous PCM build or channel count')
    detector = detector_class(config_class(f_min_hz=args.f_min_hz, f_max_hz=args.f_max_hz))
    packet_period_ms = 1000 * base.FRAMES_PER_DESCRIPTOR / base.PCM_RATE_HZ
    target_packets = math.ceil(args.duration_seconds * base.PCM_RATE_HZ / base.FRAMES_PER_DESCRIPTOR)
    metrics = {'packet_total_cpu_ms': [], 'capture_plus_detector_cpu_ms': [],
               'read_packet_cpu_ms': [], 'detector_only_cpu_ms': [],
               'diagnostic_spectrum_cpu_ms': [], 'queue_age_ms': [],
               'arrival_interval_ms': []}
    previous_return_ns = None
    system_start = base.system_cpu_counters()
    capture.enable_clock(timeout=2.0)
    start_info = capture.start(source='microphones',
                               frames_per_descriptor=base.FRAMES_PER_DESCRIPTOR,
                               slot_count=base.SLOT_COUNT, timeout=5.0)
    report['capture_start'] = start_info
    base.save_json(status_path, report)
    wall_start_ns = time.monotonic_ns()
    process_start_ns = time.process_time_ns()
    print('EXPLORATORY_CAPTURE_READY label={}'.format(args.label), flush=True)
    log_path = args.output / 'packets.jsonl'
    try:
        with log_path.open('w', encoding='utf-8', buffering=1) as log:
            for index in range(target_packets):
                cpu_start_ns = time.process_time_ns()
                packet = capture.read_packet(timeout=3.0)
                return_ns = time.monotonic_ns()
                read_end_cpu_ns = time.process_time_ns()
                metadata = packet.metadata
                if (metadata['packet_index'] != index or
                        metadata['first_frame'] != index * base.FRAMES_PER_DESCRIPTOR or
                        metadata['frames'] != base.FRAMES_PER_DESCRIPTOR or
                        packet.samples.shape != (base.FRAMES_PER_DESCRIPTOR, 4) or
                        packet.samples.dtype != np.dtype('int32')):
                    raise RuntimeError('PCM packet order, length or format changed')
                windows = detector.process_block(packet.samples)
                detector_end_cpu_ns = time.process_time_ns()
                detector_end_wall_ns = time.monotonic_ns()
                spectrum = packet_spectrum(packet.samples, args.tone_hz,
                                           args.f_min_hz, args.f_max_hz)
                track = tone_track(packet.samples, args.tone_hz)
                diagnostics_end_cpu_ns = time.process_time_ns()
                counter_frames = int(metadata['counters']['frames'])
                queued_frames = max(0, counter_frames - metadata['first_frame'] - metadata['frames'])
                queue_age_ms = queued_frames * 1000.0 / base.PCM_RATE_HZ
                interval_ms = None if previous_return_ns is None else (return_ns - previous_return_ns) / 1e6
                record = {
                    'packet_index': index,
                    'first_frame': metadata['first_frame'],
                    'returned_monotonic_ns': return_ns,
                    'detector_finished_monotonic_ns': detector_end_wall_ns,
                    'return_interval_ms': interval_ms,
                    'counter_frames': counter_frames,
                    'queue_age_estimate_ms_at_return': queue_age_ms,
                    'read_packet_cpu_ms': (read_end_cpu_ns - cpu_start_ns) / 1e6,
                    'detector_only_cpu_ms': (detector_end_cpu_ns - read_end_cpu_ns) / 1e6,
                    'diagnostic_spectrum_cpu_ms': (diagnostics_end_cpu_ns - detector_end_cpu_ns) / 1e6,
                    'capture_plus_detector_cpu_ms': (detector_end_cpu_ns - cpu_start_ns) / 1e6,
                    'windows': windows,
                    'spectrum': spectrum,
                    'tone_track_256_frame_dbfs': track,
                }
                log.write(json.dumps(record, allow_nan=False) + '\n')
                total_cpu_ms = (time.process_time_ns() - cpu_start_ns) / 1e6
                metrics['packet_total_cpu_ms'].append(total_cpu_ms)
                for key in ('capture_plus_detector_cpu_ms', 'read_packet_cpu_ms',
                            'detector_only_cpu_ms', 'diagnostic_spectrum_cpu_ms'):
                    metrics[key].append(record[key])
                metrics['queue_age_ms'].append(queue_age_ms)
                if interval_ms is not None:
                    metrics['arrival_interval_ms'].append(interval_ms)
                previous_return_ns = return_ns
                del packet
                if (index + 1) % 64 == 0:
                    print('EXPLORATORY_PROGRESS packets={}'.format(index + 1), flush=True)
        wall_end_ns = time.monotonic_ns()
        process_end_ns = time.process_time_ns()
        system_end = base.system_cpu_counters()
        final = capture.stop(timeout=8.0).metadata
        if (final['source'] != 'microphones' or final['overflows'] != 0 or
                final['clips'] != 0 or
                final['packets_received'] != final['packets_returned'] + final['packets_drained_without_return'] or
                final['packets_returned'] != target_packets):
            raise RuntimeError('Capture final counters show loss, clipping or wrong source')
        elapsed = (wall_end_ns - wall_start_ns) / 1e9
        summary = {key: base.summarize(value) for key, value in metrics.items()}
        summary.update({
            'packets_returned': target_packets,
            'elapsed_seconds_before_stop': elapsed,
            'expected_packet_period_ms': packet_period_ms,
            'process_cpu_percent_one_core': 100 * (process_end_ns - process_start_ns) / (wall_end_ns - wall_start_ns),
            'system_cpu_busy_percent': base.system_cpu_busy_percent(system_start, system_end),
            'final_capture': final,
            'zero_loss': True,
            'queue_growth_assessment': 'inspect per-packet queue trace; summary alone does not establish trend',
            'within_existing_70ms_p95_cpu_budget': summary['capture_plus_detector_cpu_ms']['p95'] <= 70,
            'diagnostics_excluded_from_product_cpu_budget': True,
        })
        report['measurement'] = summary
        report['status'] = 'measured_exploratory'
    except BaseException:
        if capture._state in ('starting', 'running', 'stopping', 'faulted'):
            try:
                capture.recover()
                report['capture_recovery'] = 'ok'
            except BaseException as error:
                report['capture_recovery'] = '{}: {}'.format(type(error).__name__, error)
        raise
    finally:
        if capture._state in ('idle', 'stopped') and capture.clock.clock_enabled:
            capture.disable_clock()
        base.save_json(status_path, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--duration-seconds', type=int, default=12)
    parser.add_argument('--tone-hz', type=float)
    parser.add_argument('--f-min-hz', type=float, default=1000)
    parser.add_argument('--f-max-hz', type=float, default=3000)
    parser.add_argument('--microphones-confirmed', action='store_true', required=True)
    args = parser.parse_args()
    if not 8 <= args.duration_seconds <= 120:
        raise ValueError('Duration must be between 8 and 120 seconds')
    if not (0 < args.f_min_hz < args.f_max_hz < base.PCM_RATE_HZ / 2):
        raise ValueError('Invalid analysis band')
    if args.tone_hz is not None and not args.f_min_hz <= args.tone_hz <= args.f_max_hz:
        raise ValueError('Expected tone must be inside analysis band')
    args.bit = args.bit.resolve()
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    status_path = args.output / 'status.json'
    if (base.sha256(args.bit) != base.BIT_SHA256 or
            base.sha256(args.bit.with_suffix('.hwh')) != base.HWH_SHA256):
        raise RuntimeError('Candidate BIT/HWH differs from pinned Phase 7 artifact')
    if not (args.source / 'pynq_acoustic_camera/single_tone.py').is_file():
        raise FileNotFoundError('Staged detector source is missing')
    sys.path.insert(0, str(args.source))
    report = {
        'phase': 8, 'step': '8.5', 'scope': 'physical_exploratory_not_acceptance',
        'status': 'running', 'label': args.label, 'tone_hz': args.tone_hz,
        'band_hz': [args.f_min_hz, args.f_max_hz],
        'duration_requested_seconds': args.duration_seconds,
        'board_wall_clock_trusted': False,
        'measurement_clock': 'monotonic_ns and process_time_ns',
        'microphones_user_confirmed_connected_at_execution': args.microphones_confirmed,
        'raw_pcm_saved': False,
        'runner_sha256': base.sha256(Path(__file__)),
        'detector_sha256': base.sha256(args.source / 'pynq_acoustic_camera/single_tone.py'),
        'candidate_bit_sha256': base.BIT_SHA256,
        'candidate_hwh_sha256': base.HWH_SHA256,
        'cpu_budget_p95_ms_per_packet': 70,
        'latency_budget_ms': 200,
        'latency_limitation': '256-frame matched-frequency PCM trace permits an exploratory onset estimate; no synchronized external marker, so physical onset-to-output latency is not certified.',
        'snr_limitation': 'Tone-to-other-band ratio is an approximate per-packet estimate, not a calibrated acoustic SNR.',
    }
    base.save_json(status_path, report)
    shutil.copy2(Path(__file__), args.output / 'runner.py')
    base_attempted = False
    base_restored = False
    measured = False
    try:
        if os.geteuid() != 0 or sys.prefix != '/usr/local/share/pynq-venv' or os.environ.get('XILINX_XRT') != '/usr':
            raise RuntimeError('Run as root with PYNQ venv Python and XILINX_XRT=/usr')
        board = Path('/proc/device-tree/chosen/pynq_board').read_bytes().rstrip(b'\0').decode()
        if board.lower().replace('-', '_') != 'pynq_z2':
            raise RuntimeError('Expected PYNQ-Z2, got ' + board)
        from pynq import Overlay
        from pynq.pl_server.device import Device
        import pynq
        from pynq_acoustic_camera import ContinuousPcmCapture
        from pynq_acoustic_camera.continuous_capture import _UNSAFE_CONTINUOUS_BUFFERS
        from pynq_acoustic_camera.single_tone import StreamingSingleToneDetector, ToneDetectorConfig

        base_bit = Path(pynq.__file__).parent / 'overlays/base/base.bit'
        if (base.sha256(base_bit) != base.BASE_BIT_SHA256 or
                base.sha256(base_bit.with_suffix('.hwh')) != base.BASE_HWH_SHA256):
            raise RuntimeError('Official Base BIT/HWH hashes differ')
        state_file = Path(pynq.__file__).parent / 'pl_server/global_pl_state_.json'
        fpga_state = Path('/sys/class/fpga_manager/fpga0/state')
        fpga_flags = Path('/sys/class/fpga_manager/fpga0/flags')
        base_attempted = True
        Overlay(str(base_bit), download=True, device=Device.active_device)
        report['base_before_candidate'] = base.verify_loaded(
            base_bit, base.BASE_BIT_SHA256, state_file, fpga_state, fpga_flags)
        overlay = Overlay(str(args.bit), download=True, device=Device.active_device)
        report['candidate_loaded'] = base.verify_loaded(
            args.bit, base.BIT_SHA256, state_file, fpga_state, fpga_flags)
        measure(ContinuousPcmCapture, StreamingSingleToneDetector, ToneDetectorConfig,
                overlay, args, report, status_path)
        measured = True
    except BaseException as error:
        report['status'] = 'fail'
        report['error'] = '{}: {}'.format(type(error).__name__, error)
        report['traceback'] = traceback.format_exc()
    finally:
        if base_attempted:
            try:
                Overlay(str(base_bit), download=True, device=Device.active_device)
                report['base_restored'] = base.verify_loaded(
                    base_bit, base.BASE_BIT_SHA256, state_file, fpga_state, fpga_flags)
                if len(_UNSAFE_CONTINUOUS_BUFFERS) != 0:
                    raise RuntimeError('Unsafe DMA buffers remain retained')
                base_restored = True
            except BaseException as error:
                report['base_restore_error'] = '{}: {}'.format(type(error).__name__, error)
        report['base_restored_ok'] = base_restored if base_attempted else None
        report['status'] = 'measured_exploratory' if measured and base_restored else 'fail'
        base.save_json(status_path, report)
    print('PHASE8_EXPLORATORY_RESULT=' + report['status'], flush=True)
    return 0 if report['status'] == 'measured_exploratory' else 1


if __name__ == '__main__':
    raise SystemExit(main())
