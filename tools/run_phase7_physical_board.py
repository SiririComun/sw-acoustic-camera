#!/usr/bin/env python3
"""Run the Phase 7.6 prolonged physical four-microphone PCM capture.

The runner loads the pinned Phase 7 continuous candidate, captures live
microphone PCM through S2MM Scatter/Gather, then restores the verified PYNQ
Base overlay. It stores packet metadata, per-packet hashes, and channel
statistics, but not the raw audio.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import traceback

from run_phase7_synthetic_board import (
    BASE_BIT_SHA256,
    BASE_HWH_SHA256,
    BIT_SHA256,
    BUILD_ID,
    FRAMES_PER_DESCRIPTOR,
    HWH_SHA256,
    PCM_RATE_HZ,
    SLOT_COUNT,
    inspect_retained_dma_buffers,
    sha256,
    utc_now,
    verify_loaded,
)


def save_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def new_channel_stats():
    return [
        {'min': None, 'max': None, 'sum': 0, 'sum_squares': 0.0,
         'zero_samples': 0, 'varying_packets': 0, 'frames': 0}
        for _ in range(4)
    ]


def add_packet_stats(stats, samples):
    import numpy as np

    if samples.shape != (FRAMES_PER_DESCRIPTOR, 4) or samples.dtype != np.dtype('int32'):
        raise RuntimeError('Physical PCM packet has an unexpected shape or dtype')
    for channel in range(4):
        values = samples[:, channel]
        low, high = int(values.min()), int(values.max())
        item = stats[channel]
        item['min'] = low if item['min'] is None else min(item['min'], low)
        item['max'] = high if item['max'] is None else max(item['max'], high)
        item['sum'] += int(values.sum(dtype=np.int64))
        as_float = values.astype(np.float64)
        item['sum_squares'] += float(np.dot(as_float, as_float))
        item['zero_samples'] += int(np.count_nonzero(values == 0))
        item['varying_packets'] += int(low != high)
        item['frames'] += int(values.size)


def finalize_channel_stats(stats):
    result = []
    for channel, item in enumerate(stats):
        frames = item['frames']
        result.append({
            'channel': channel,
            'frames_analyzed': frames,
            'min': item['min'],
            'max': item['max'],
            'mean': item['sum'] / frames if frames else None,
            'rms': math.sqrt(item['sum_squares'] / frames) if frames else None,
            'zero_samples': item['zero_samples'],
            'varying_packets': item['varying_packets'],
            'nonconstant_samples_observed': bool(frames and item['min'] != item['max']),
        })
    return result


def read_fault_registers(capture):
    dma, control = capture._dma, capture._control
    return {
        'dma': {
            'control': dma.read(0x30), 'status': dma.read(0x34),
            'current_descriptor': dma.read(0x38),
            'current_descriptor_msb': dma.read(0x3C),
            'tail_descriptor': dma.read(0x40),
            'tail_descriptor_msb': dma.read(0x44),
        },
        'capture': {
            'status': control.read(0x28), 'error_flags': control.read(0x2C),
            'format': control.read(0x30), 'source_pattern': control.read(0x34),
            'frames_produced': control.read(0x4C), 'words_sent': control.read(0x50),
            'overflow_count': control.read(0x54), 'clip_count': control.read(0x58),
            'total_frames_low': control.read(0x68), 'total_frames_high': control.read(0x6C),
            'total_words_low': control.read(0x70), 'total_words_high': control.read(0x74),
        },
    }


def run_capture(overlay, capture_class, args, report, status_path):
    capture = capture_class.from_overlay(
        overlay,
        provenance={
            'phase_step': '7.6',
            'source': 'physical_microphones',
            'candidate_build_id': BUILD_ID,
            'hardware_commit': args.hardware_commit,
            'software_commit': args.software_commit,
        },
    )
    identity = capture.clock.identity
    if identity.build_id != BUILD_ID or identity.channels != 4:
        raise RuntimeError('Continuous candidate identity/channel count mismatch')

    capture.enable_clock(timeout=2.0)
    target_packets = math.ceil(args.duration_seconds * PCM_RATE_HZ / FRAMES_PER_DESCRIPTOR)
    start_info = capture.start(
        source='microphones', frames_per_descriptor=FRAMES_PER_DESCRIPTOR,
        slot_count=SLOT_COUNT, timeout=5.0,
    )
    run = {
        'status': 'running',
        'started_utc': utc_now(),
        'requested_duration_seconds': args.duration_seconds,
        'target_packets': target_packets,
        'source': 'microphones',
        'start': start_info,
        'identity': {
            'magic': identity.magic,
            'abi_version': identity.abi_version,
            'capabilities': identity.capabilities,
            'channels': identity.channels,
            'build_id': identity.build_id,
        },
        'packets': [],
        'channel_accumulators': new_channel_stats(),
        'health_after_start': capture._diagnostics(),
    }
    report['physical_capture'] = run
    save_json(status_path, report)
    started = time.monotonic()
    previous_counter_frames = 0
    previous_counter_words = 0

    try:
        for index in range(target_packets):
            packet = capture.read_packet(timeout=3.0)
            metadata = packet.metadata
            expected_first_frame = index * FRAMES_PER_DESCRIPTOR
            if metadata['packet_index'] != index:
                raise RuntimeError('Physical packet index has a gap or duplicate')
            if metadata['first_frame'] != expected_first_frame:
                raise RuntimeError('Physical PCM frame sequence is discontinuous at a buffer boundary')
            if metadata['frames'] != FRAMES_PER_DESCRIPTOR or metadata['bytes'] != FRAMES_PER_DESCRIPTOR * 16:
                raise RuntimeError('Physical PCM descriptor has an incomplete frame/byte count')
            if metadata['source'] != 'microphones' or metadata['build_id'] != BUILD_ID:
                raise RuntimeError('Packet source or candidate identity changed during capture')
            counter_frames = metadata['counters']['frames']
            counter_words = metadata['counters']['words']
            if (counter_frames < previous_counter_frames
                    or counter_words < previous_counter_words
                    or counter_words > counter_frames * 4
                    or counter_words < (index + 1) * FRAMES_PER_DESCRIPTOR * 4):
                raise RuntimeError('Hardware frame/word snapshots regressed or disagree')
            previous_counter_frames = counter_frames
            previous_counter_words = counter_words
            add_packet_stats(run['channel_accumulators'], packet.samples)
            run['packets'].append({
                'packet_index': index,
                'first_frame': metadata['first_frame'],
                'frames': metadata['frames'],
                'bytes': metadata['bytes'],
                'descriptor_status': metadata['descriptor_status'],
                'counter_frames': counter_frames,
                'counter_words': counter_words,
                'slot_index': index % SLOT_COUNT,
                'pcm_sha256': hashlib.sha256(packet.samples.tobytes(order='C')).hexdigest(),
            })
            if (index + 1) % 64 == 0:
                print('PHYSICAL_PROGRESS packets={}'.format(index + 1), flush=True)
                save_json(status_path, report)

        elapsed = time.monotonic() - started
        result = capture.stop(timeout=8.0).metadata
        if elapsed < args.duration_seconds:
            raise RuntimeError('Physical capture ended before the requested duration')
        if result['source'] != 'microphones':
            raise RuntimeError('Final capture source is not the physical microphones')
        if result['packets_received'] < target_packets:
            raise RuntimeError('Physical capture stopped before the requested packet count')
        if result['packets_received'] != result['packets_returned'] + result['packets_drained_without_return']:
            raise RuntimeError('Returned and drained descriptor counts do not add up')
        if result['frames_total'] != result['packets_received'] * FRAMES_PER_DESCRIPTOR:
            raise RuntimeError('Final frame total does not match the complete descriptor sequence')
        if result['frames_total'] * 16 != result['bytes_received']:
            raise RuntimeError('Final frame count differs from DMA byte total')
        if result['words_total'] * 4 != result['bytes_received']:
            raise RuntimeError('Final word count differs from DMA byte total')
        if result['frames_total'] * 4 != result['words_total']:
            raise RuntimeError('Final four-channel frame/word counts disagree')
        if result['overflows'] != 0 or result['clips'] != 0:
            raise RuntimeError('Physical capture reported overflow or PCM clips')
        if result['cyclic_dma_mode']:
            raise RuntimeError('DMA cyclic mode was unexpectedly enabled')

        run['channel_stats'] = finalize_channel_stats(run.pop('channel_accumulators'))
        run.update({
            'status': 'pass',
            'finished_utc': utc_now(),
            'elapsed_seconds': elapsed,
            'packets_per_second': len(run['packets']) / elapsed,
            'descriptor_ring_wraps': result['packets_received'] // SLOT_COUNT,
            'all_channels_nonconstant': all(
                channel['nonconstant_samples_observed'] for channel in run['channel_stats']),
            'result': result,
        })
        print('PHYSICAL_PASS packets={} frames={} elapsed={:.3f}s overflows={} clips={}'.format(
            result['packets_received'], result['frames_total'], elapsed,
            result['overflows'], result['clips']), flush=True)
        if not run['all_channels_nonconstant']:
            run['channel_activity_note'] = 'At least one PCM channel was constant during returned packets; review physical signal separately.'
        return capture
    except BaseException as error:
        run.update({
            'status': 'fail',
            'finished_utc': utc_now(),
            'error': '{}: {}'.format(type(error).__name__, error),
            'traceback': traceback.format_exc(),
        })
        diagnostics = getattr(error, 'diagnostics', None)
        if diagnostics is not None:
            run['error_diagnostics'] = diagnostics
        try:
            run['fault_registers'] = read_fault_registers(capture)
        except Exception as diagnostic_error:
            run['fault_registers_error'] = '{}: {}'.format(
                type(diagnostic_error).__name__, diagnostic_error)
        if capture._state in ('starting', 'running', 'stopping', 'faulted'):
            try:
                capture.recover()
                run['recovery'] = 'ok'
            except Exception as recovery_error:
                run['recovery'] = '{}: {}'.format(type(recovery_error).__name__, recovery_error)
        raise
    finally:
        if capture._state == 'stopped' and capture.clock.clock_enabled:
            capture.disable_clock()
        elif capture._state == 'idle' and capture.clock.clock_enabled:
            capture.disable_clock()
        save_json(status_path, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=int, default=30)
    parser.add_argument('--hardware-commit', required=True)
    parser.add_argument('--software-commit', required=True)
    args = parser.parse_args()
    args.bit = args.bit.resolve()
    args.output = args.output.resolve()
    args.source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    status_path = args.output / 'status.json'
    if not (args.source / 'pynq_acoustic_camera' / '__init__.py').is_file():
        raise FileNotFoundError('Staged pynq_acoustic_camera source package is missing')
    sys.path.insert(0, str(args.source))

    report = {
        'phase': 7,
        'step': '7.6',
        'scope': 'phase_acceptance',
        'status': 'running',
        'started_utc': utc_now(),
        'board_programming': 'candidate_replaces_active_overlay_then_official_base_restored',
        'microphones_required': True,
        'microphones_user_confirmed_connected': True,
        'microphone_map': {
            'shared_pdm_clock': 'Pmod B1',
            'channel_data': ['Pmod B2', 'Pmod B3', 'Pmod B4', 'Pmod B7'],
        },
        'raw_audio_saved': False,
        'candidate_build_id': BUILD_ID,
        'candidate_bit_sha256': sha256(args.bit),
        'candidate_hwh_sha256': sha256(args.bit.with_suffix('.hwh')),
        'hardware_commit': args.hardware_commit,
        'software_commit': args.software_commit,
        'software_source': str(args.source),
        'requested_duration_seconds': args.duration_seconds,
        'runner_sha256': sha256(Path(__file__)),
        'python': sys.executable,
        'python_prefix': sys.prefix,
        'uid': os.geteuid(),
        'xilinx_xrt': os.environ.get('XILINX_XRT'),
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'started_board_utc': utc_now(),
    }
    save_json(status_path, report)
    shutil.copy2(Path(__file__), args.output / 'runner.py')
    base_restored = False
    candidate_load_attempted = False
    capture = None

    try:
        if os.geteuid() != 0:
            raise RuntimeError('Run this board capture as root')
        if sys.prefix != '/usr/local/share/pynq-venv':
            raise RuntimeError('Use the PYNQ virtualenv Python')
        if os.environ.get('XILINX_XRT') != '/usr':
            raise RuntimeError('XILINX_XRT must be /usr')
        if args.duration_seconds < 30:
            raise ValueError('Use at least 30 seconds for prolonged physical capture')
        if report['candidate_bit_sha256'] != BIT_SHA256 or report['candidate_hwh_sha256'] != HWH_SHA256:
            raise RuntimeError('Candidate BIT/HWH does not match the reviewed artifact hashes')

        board = Path('/proc/device-tree/chosen/pynq_board').read_bytes().rstrip(b'\0').decode()
        if board.lower().replace('-', '_') != 'pynq_z2':
            raise RuntimeError('Expected PYNQ-Z2, device tree reports ' + board)
        report['board'] = board

        from pynq import Overlay
        from pynq.pl_server.device import Device
        import pynq

        pynq_root = Path(pynq.__file__).parent
        base_bit = pynq_root / 'overlays/base/base.bit'
        base_hwh = base_bit.with_suffix('.hwh')
        if sha256(base_bit) != BASE_BIT_SHA256 or sha256(base_hwh) != BASE_HWH_SHA256:
            raise RuntimeError('Installed official PYNQ-Z2 Base files fail their pinned hashes')
        state_file = pynq_root / 'pl_server/global_pl_state_.json'
        fpga_state = Path('/sys/class/fpga_manager/fpga0/state')
        fpga_flags = Path('/sys/class/fpga_manager/fpga0/flags')
        report['base_bit'] = str(base_bit)
        report['base_bit_sha256'] = BASE_BIT_SHA256
        report['base_hwh_sha256'] = BASE_HWH_SHA256
        if not state_file.is_file():
            # A cold boot can leave the FPGA running while PYNQ has no saved
            # overlay identity. Load the pinned official Base once to establish
            # a verifiable starting point before replacing it with the candidate.
            print('BASE_STATE_MISSING_LOADING_OFFICIAL_BASE', flush=True)
            Overlay(str(base_bit), download=True, device=Device.active_device)
        report['active_before'] = verify_loaded(
            base_bit, BASE_BIT_SHA256, state_file, fpga_state, fpga_flags)
        print('ACTIVE_BASE_VERIFIED', flush=True)

        candidate_load_attempted = True
        overlay = Overlay(str(args.bit), download=True, device=Device.active_device)
        report['candidate_loaded'] = verify_loaded(
            args.bit, BIT_SHA256, state_file, fpga_state, fpga_flags)
        report['pynq_version'] = pynq.__version__
        print('CANDIDATE_LOADED build_id_expected=' + BUILD_ID, flush=True)

        from pynq_acoustic_camera import ContinuousPcmCapture
        capture = run_capture(overlay, ContinuousPcmCapture, args, report, status_path)
        report['status'] = 'pass'
    except BaseException as error:
        report.update({
            'status': 'fail',
            'error': '{}: {}'.format(type(error).__name__, error),
            'traceback': traceback.format_exc(),
        })
        diagnostics = getattr(error, 'diagnostics', None)
        if diagnostics is not None:
            report['error_diagnostics'] = diagnostics
        print('STEP7_6_ERROR=' + report['error'], flush=True)
    finally:
        if candidate_load_attempted:
            try:
                from pynq import Overlay
                from pynq.pl_server.device import Device
                import pynq

                base_bit = Path(pynq.__file__).parent / 'overlays/base/base.bit'
                if sha256(base_bit) != BASE_BIT_SHA256:
                    raise RuntimeError('Refusing to restore an unverified Base bitstream')
                Overlay(str(base_bit), download=True, device=Device.active_device)
                state_file = Path(pynq.__file__).parent / 'pl_server/global_pl_state_.json'
                report['base_restored'] = verify_loaded(
                    base_bit, BASE_BIT_SHA256, state_file,
                    Path('/sys/class/fpga_manager/fpga0/state'),
                    Path('/sys/class/fpga_manager/fpga0/flags'))
                base_restored = True
                print('OFFICIAL_BASE_RESTORED', flush=True)
            except BaseException as restore_error:
                report['base_restore_error'] = '{}: {}'.format(
                    type(restore_error).__name__, restore_error)
                report['status'] = 'fail'
                print('BASE_RESTORE_ERROR=' + report['base_restore_error'], flush=True)
            if base_restored:
                try:
                    report['retained_dma_buffers_after_base_restore'] = inspect_retained_dma_buffers()
                except BaseException as diagnostic_error:
                    report['retained_dma_inspection_error'] = '{}: {}'.format(
                        type(diagnostic_error).__name__, diagnostic_error)
        report['base_restored_ok'] = base_restored if candidate_load_attempted else None
        report['finished_utc'] = utc_now()
        save_json(status_path, report)

    print('STEP7_6_RESULT=' + report['status'], flush=True)
    return 0 if report['status'] == 'pass' and base_restored else 1


if __name__ == '__main__':
    raise SystemExit(main())
