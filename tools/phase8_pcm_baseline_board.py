#!/usr/bin/env python3
"""Measure the no-DSP PCM delivery baseline on the PYNQ-Z2.

This phase-8.1 runner records packet timing, driver CPU time, frame-counter
progress, queue age, and loss/error counters. It never saves PCM samples. It
loads the pinned Phase 7 capture BIT only after explicitly loading the
hash-verified official PYNQ Base overlay, and restores Base in a finally block.
Run with the PYNQ virtualenv Python as root.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import sys
import time
import traceback


BUILD_ID = '7A2C7F017A2C4E7D8F70070100000003'
BIT_SHA256 = '679d890f92c9ae7d13b6fb71d2777a6e3c89d2c375f1cee10f04fec970b7a56a'
HWH_SHA256 = 'e1cb95983851088f5cd22e1b8a107eab5462ac24eaf33bb31f8eb563dacf0833'
BASE_BIT_SHA256 = '3239e9cc37ee4993c25f02ae6c97da0b3372f0e722e267d10073e20adc5d6717'
BASE_HWH_SHA256 = '222898ef2c0ccc4a0c47608e99d8dc7c9c6cdfed5130964884ff35005827ef3a'
PCM_RATE_HZ = 48000
FRAMES_PER_DESCRIPTOR = 4096
SLOT_COUNT = 64


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(temp, path)


def verify_loaded(path, expected_sha256, state_file, fpga_state, fpga_flags):
    state = json.loads(state_file.read_text(encoding='utf-8'))
    requested = Path(path).resolve()
    loaded = Path(state.get('bitfile_name', '')).resolve()
    manager_state = fpga_state.read_text(encoding='ascii').strip()
    manager_flags = fpga_flags.read_text(encoding='ascii').strip()
    if loaded != requested:
        raise RuntimeError('PYNQ global PL state does not match the requested BIT')
    if manager_state != 'operating' or manager_flags not in ('0', '0x0'):
        raise RuntimeError('FPGA manager is not operating cleanly')
    if sha256(path) != expected_sha256:
        raise RuntimeError('Loaded BIT SHA-256 differs from the pinned artifact')
    return {
        'bitfile_name': str(loaded),
        'bitfile_sha256': expected_sha256,
        'pynq_active_name': state.get('active_name'),
        'fpga_manager_state': manager_state,
        'fpga_manager_flags': manager_flags,
    }


def percentile(values, p):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * p / 100.0
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def summarize(values):
    if not values:
        return {'count': 0, 'min': None, 'p50': None, 'p95': None, 'max': None}
    converted = [float(value) for value in values]
    return {
        'count': len(converted),
        'min': min(converted),
        'p50': percentile(converted, 50),
        'p95': percentile(converted, 95),
        'max': max(converted),
    }


def system_cpu_counters():
    fields = Path('/proc/stat').read_text(encoding='ascii').splitlines()[0].split()
    values = [int(item) for item in fields[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return {'total': sum(values), 'idle': idle}


def system_cpu_busy_percent(before, after):
    total = after['total'] - before['total']
    idle = after['idle'] - before['idle']
    if total <= 0:
        return None
    return 100.0 * max(0, total - idle) / total


def run_baseline(capture_class, overlay, args, report, status_path):
    capture = capture_class.from_overlay(
        overlay,
        provenance={
            'phase_step': '8.1',
            'source': 'physical_microphones_no_dsp',
            'candidate_build_id': BUILD_ID,
            'hardware_commit': args.hardware_commit,
            'software_commit': args.software_commit,
        },
    )
    identity = capture.clock.identity
    if identity.build_id != BUILD_ID or identity.channels != 4:
        raise RuntimeError('Continuous candidate identity/channel count mismatch')

    capture.enable_clock(timeout=2.0)
    start_info = capture.start(
        source='microphones',
        frames_per_descriptor=FRAMES_PER_DESCRIPTOR,
        slot_count=SLOT_COUNT,
        timeout=5.0,
    )
    run = {
        'status': 'running',
        'started_board_utc': utc_now(),
        'measurement_clock': 'time.monotonic_ns and time.process_time_ns',
        'duration_requested_seconds': args.duration_seconds,
        'source': 'microphones',
        'dsp_enabled': False,
        'start': start_info,
        'identity': {
            'abi_version': identity.abi_version,
            'capabilities': identity.capabilities,
            'channels': identity.channels,
            'build_id': identity.build_id,
        },
        'expected_packet_period_ms': 1000.0 * FRAMES_PER_DESCRIPTOR / PCM_RATE_HZ,
        'packets': [],
        'baseline_latency_measurement': 'detector_not_present; detection latency is not measured in step 8.1',
    }
    report['pcm_baseline'] = run
    save_json(status_path, report)

    target_packets = math.ceil(args.duration_seconds * PCM_RATE_HZ / FRAMES_PER_DESCRIPTOR)
    previous_return_ns = None
    previous_counter_frames = None
    frame_gaps = 0
    counter_regressions = 0
    read_wall_ms = []
    read_cpu_ms = []
    arrival_intervals_ms = []
    counter_rates_hz = []
    queue_lag_ms = []
    wall_start_ns = time.monotonic_ns()
    cpu_start_ns = time.process_time_ns()
    system_cpu_start = system_cpu_counters()

    try:
        for index in range(target_packets):
            call_start_wall = time.monotonic_ns()
            call_start_cpu = time.process_time_ns()
            packet = capture.read_packet(timeout=3.0)
            call_end_cpu = time.process_time_ns()
            call_end_wall = time.monotonic_ns()
            metadata = packet.metadata
            expected_first_frame = index * FRAMES_PER_DESCRIPTOR
            if metadata['packet_index'] != index or metadata['first_frame'] != expected_first_frame:
                frame_gaps += 1
                raise RuntimeError('PCM packet/frame sequence is discontinuous')
            if (metadata['frames'] != FRAMES_PER_DESCRIPTOR
                    or metadata['bytes'] != FRAMES_PER_DESCRIPTOR * 16
                    or packet.samples.shape != (FRAMES_PER_DESCRIPTOR, 4)
                    or str(packet.samples.dtype) != 'int32'):
                raise RuntimeError('PCM packet shape, dtype, or descriptor length is invalid')

            counters = metadata['counters']
            counter_frames = int(counters['frames'])
            if previous_counter_frames is not None:
                delta_frames = counter_frames - previous_counter_frames
                delta_time_ns = call_end_wall - previous_return_ns
                if delta_frames < 0:
                    counter_regressions += 1
                elif delta_time_ns > 0:
                    counter_rates_hz.append(delta_frames * 1e9 / delta_time_ns)
                    if delta_frames > 0:
                        arrival_intervals_ms.append(delta_time_ns / 1e6)
            else:
                delta_frames = counter_frames
            read_wall_ms.append((call_end_wall - call_start_wall) / 1e6)
            read_cpu_ms.append((call_end_cpu - call_start_cpu) / 1e6)
            lag_frames = max(0, counter_frames - (metadata['first_frame'] + metadata['frames']))
            queue_lag_ms.append(lag_frames * 1000.0 / PCM_RATE_HZ)
            run['packets'].append({
                'packet_index': index,
                'first_frame': metadata['first_frame'],
                'frames': metadata['frames'],
                'bytes': metadata['bytes'],
                'arrival_elapsed_ms': (call_end_wall - wall_start_ns) / 1e6,
                'arrival_interval_ms': (None if previous_return_ns is None else
                                        (call_end_wall - previous_return_ns) / 1e6),
                'read_packet_wall_ms': (call_end_wall - call_start_wall) / 1e6,
                'read_packet_process_cpu_ms': (call_end_cpu - call_start_cpu) / 1e6,
                'counter_frames': counter_frames,
                'counter_frames_delta': delta_frames,
                'estimated_queued_frames_at_return': lag_frames,
                'estimated_queue_age_ms_at_return': queue_lag_ms[-1],
            })
            previous_return_ns = call_end_wall
            previous_counter_frames = counter_frames
            del packet
            if (index + 1) % 64 == 0:
                print('BASELINE_PROGRESS packets={}'.format(index + 1), flush=True)

        wall_end_ns = time.monotonic_ns()
        cpu_end_ns = time.process_time_ns()
        system_cpu_end = system_cpu_counters()
        result = capture.stop(timeout=8.0).metadata
        if result['source'] != 'microphones':
            raise RuntimeError('Final source was not the physical microphones')
        if result['packets_received'] != result['packets_returned'] + result['packets_drained_without_return']:
            raise RuntimeError('Returned and drained descriptor counts disagree')
        if result['frames_total'] * 16 != result['bytes_received']:
            raise RuntimeError('Final frame and byte totals disagree')
        if result['words_total'] * 4 != result['bytes_received']:
            raise RuntimeError('Final word and byte totals disagree')
        if result['overflows'] != 0 or result['clips'] != 0:
            raise RuntimeError('PCM capture reported overflow or clips')

        elapsed = (wall_end_ns - wall_start_ns) / 1e9
        process_cpu = (cpu_end_ns - cpu_start_ns) / 1e9
        run.update({
            'status': 'pass',
            'finished_board_utc': utc_now(),
            'packets_returned_to_consumer': len(run['packets']),
            'elapsed_seconds_before_stop_drain': elapsed,
            'read_packet_wall_ms': summarize(read_wall_ms),
            'read_packet_process_cpu_ms': summarize(read_cpu_ms),
            'packet_return_interval_ms': summarize(arrival_intervals_ms),
            'counter_delta_rate_estimate_hz': summarize(counter_rates_hz),
            'queue_age_estimate_ms': summarize(queue_lag_ms),
            'capture_loop_process_cpu_seconds': process_cpu,
            'capture_loop_process_cpu_percent_of_one_core': 100.0 * process_cpu / elapsed if elapsed else None,
            'system_cpu_busy_percent': system_cpu_busy_percent(system_cpu_start, system_cpu_end),
            'sequence_gaps': frame_gaps,
            'counter_regressions': counter_regressions,
            'final_result': result,
        })
        print('PCM_BASELINE_CAPTURE_PASS packets={} frames={} elapsed={:.3f}s overflows={} clips={}'.format(
            len(run['packets']), result['frames_total'], elapsed,
            result['overflows'], result['clips']), flush=True)
        return capture
    except BaseException:
        if capture._state in ('starting', 'running', 'stopping', 'faulted'):
            try:
                capture.recover()
                run['capture_recovery'] = 'ok'
            except BaseException as recovery_error:
                run['capture_recovery'] = '{}: {}'.format(type(recovery_error).__name__, recovery_error)
        raise
    finally:
        if capture._state in ('idle', 'stopped') and capture.clock.clock_enabled:
            capture.disable_clock()
        save_json(status_path, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=int, default=20)
    parser.add_argument('--hardware-commit', required=True)
    parser.add_argument('--software-commit', required=True)
    args = parser.parse_args()
    args.bit = args.bit.resolve()
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    status_path = args.output / 'status.json'
    if args.duration_seconds < 10:
        raise ValueError('Use at least 10 seconds to gather packet timing samples')
    if not (args.source / 'pynq_acoustic_camera' / '__init__.py').is_file():
        raise FileNotFoundError('Staged pynq_acoustic_camera source package is missing')
    if sha256(args.bit) != BIT_SHA256 or sha256(args.bit.with_suffix('.hwh')) != HWH_SHA256:
        raise RuntimeError('Candidate BIT/HWH differs from the pinned Phase 7 artifact')
    sys.path.insert(0, str(args.source))

    report = {
        'phase': 8,
        'step': '8.1',
        'scope': 'no_dsp_pcm_input_reference',
        'status': 'running',
        'started_board_utc': utc_now(),
        'board_wall_clock_trusted': False,
        'board_clock_note': 'Use monotonic counters for all timing; board wall clock was observed behind the client date during preflight.',
        'board_programming': 'explicit_official_base_load_then_pinned_phase7_candidate_then_official_base_restore',
        'candidate_build_id': BUILD_ID,
        'candidate_bit_sha256': BIT_SHA256,
        'candidate_hwh_sha256': HWH_SHA256,
        'hardware_commit': args.hardware_commit,
        'software_commit': args.software_commit,
        'runner_sha256': sha256(Path(__file__)),
        'python': sys.executable,
        'python_prefix': sys.prefix,
        'uid': os.geteuid(),
        'xilinx_xrt': os.environ.get('XILINX_XRT'),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'microphones_user_confirmed_connected': True,
        'microphone_map_reported': ['Pmod B2/PCM 0', 'Pmod B3/PCM 1', 'Pmod B4/PCM 2', 'Pmod B7/PCM 3'],
        'raw_pcm_saved': False,
        'frames_per_descriptor': FRAMES_PER_DESCRIPTOR,
        'slot_count': SLOT_COUNT,
        'queue_capacity_seconds': SLOT_COUNT * FRAMES_PER_DESCRIPTOR / PCM_RATE_HZ,
        'detection_latency_measurement': 'deferred_until_detector_exists_in_step_8.4',
    }
    save_json(status_path, report)
    shutil.copy2(Path(__file__), args.output / 'runner.py')

    base_attempted = False
    base_restored = False
    measurement_ok = False
    try:
        if os.geteuid() != 0:
            raise RuntimeError('Run this board baseline as root')
        if sys.prefix != '/usr/local/share/pynq-venv':
            raise RuntimeError('Use the PYNQ virtualenv Python')
        if os.environ.get('XILINX_XRT') != '/usr':
            raise RuntimeError('XILINX_XRT must be /usr')

        board = Path('/proc/device-tree/chosen/pynq_board').read_bytes().rstrip(b'\0').decode()
        if board.lower().replace('-', '_') != 'pynq_z2':
            raise RuntimeError('Expected PYNQ-Z2, device tree reports ' + board)
        report['board'] = board

        from pynq import Overlay
        from pynq.pl_server.device import Device
        import pynq
        from pynq_acoustic_camera import ContinuousPcmCapture
        from pynq_acoustic_camera.continuous_capture import _UNSAFE_CONTINUOUS_BUFFERS

        pynq_root = Path(pynq.__file__).parent
        base_bit = pynq_root / 'overlays/base/base.bit'
        base_hwh = base_bit.with_suffix('.hwh')
        if sha256(base_bit) != BASE_BIT_SHA256 or sha256(base_hwh) != BASE_HWH_SHA256:
            raise RuntimeError('Installed official Base BIT/HWH failed pinned hashes')
        state_file = pynq_root / 'pl_server/global_pl_state_.json'
        fpga_state = Path('/sys/class/fpga_manager/fpga0/state')
        fpga_flags = Path('/sys/class/fpga_manager/fpga0/flags')
        report['base_bit_sha256'] = BASE_BIT_SHA256
        report['base_hwh_sha256'] = BASE_HWH_SHA256

        # The project requires an explicit Base download before using Base,
        # regardless of PYNQ's saved overlay state.
        base_attempted = True
        Overlay(str(base_bit), download=True, device=Device.active_device)
        report['base_before_candidate'] = verify_loaded(
            base_bit, BASE_BIT_SHA256, state_file, fpga_state, fpga_flags)
        print('OFFICIAL_BASE_EXPLICITLY_LOADED', flush=True)

        overlay = Overlay(str(args.bit), download=True, device=Device.active_device)
        report['candidate_loaded'] = verify_loaded(
            args.bit, BIT_SHA256, state_file, fpga_state, fpga_flags)
        report['pynq_version'] = pynq.__version__
        print('PHASE7_PCM_CANDIDATE_LOADED', flush=True)

        run_baseline(ContinuousPcmCapture, overlay, args, report, status_path)
        measurement_ok = True
    except BaseException as error:
        report.update({
            'status': 'fail',
            'error': '{}: {}'.format(type(error).__name__, error),
            'traceback': traceback.format_exc(),
        })
        diagnostics = getattr(error, 'diagnostics', None)
        if diagnostics is not None:
            report['error_diagnostics'] = diagnostics
        print('PHASE8_1_BASELINE_ERROR=' + report['error'], flush=True)
    finally:
        if base_attempted:
            try:
                from pynq import Overlay
                from pynq.pl_server.device import Device
                import pynq

                base_bit = Path(pynq.__file__).parent / 'overlays/base/base.bit'
                if sha256(base_bit) != BASE_BIT_SHA256:
                    raise RuntimeError('Refusing to restore an unverified Base BIT')
                Overlay(str(base_bit), download=True, device=Device.active_device)
                state_file = Path(pynq.__file__).parent / 'pl_server/global_pl_state_.json'
                report['base_restored'] = verify_loaded(
                    base_bit, BASE_BIT_SHA256, state_file,
                    Path('/sys/class/fpga_manager/fpga0/state'),
                    Path('/sys/class/fpga_manager/fpga0/flags'))
                base_restored = True
                report['unsafe_dma_buffer_roots_after_restore'] = len(
                    _UNSAFE_CONTINUOUS_BUFFERS)
                if report['unsafe_dma_buffer_roots_after_restore'] != 0:
                    raise RuntimeError('Continuous DMA buffers remain retained after Base restore')
                print('OFFICIAL_BASE_RESTORED', flush=True)
            except BaseException as restore_error:
                report['base_restore_error'] = '{}: {}'.format(
                    type(restore_error).__name__, restore_error)
                print('OFFICIAL_BASE_RESTORE_ERROR=' + report['base_restore_error'], flush=True)
        report['base_restored_ok'] = base_restored if base_attempted else None
        report['status'] = 'pass' if measurement_ok and base_restored else 'fail'
        report['finished_board_utc'] = utc_now()
        save_json(status_path, report)

    print('PHASE8_1_BASELINE_RESULT=' + report['status'], flush=True)
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
