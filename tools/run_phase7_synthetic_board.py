#!/usr/bin/env python3
"""Run the PYNQ-Z2 continuous PCM pattern and overload checks.

Run under the PYNQ virtualenv as root. The candidate replaces the active PL
overlay for this run; the verified official PYNQ-Z2 Base overlay is restored
in a finally block. The synthetic pattern does not use microphone inputs.
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


BUILD_ID = '7A2C7F017A2C4E7D8F70070100000003'
BIT_SHA256 = '679d890f92c9ae7d13b6fb71d2777a6e3c89d2c375f1cee10f04fec970b7a56a'
HWH_SHA256 = 'e1cb95983851088f5cd22e1b8a107eab5462ac24eaf33bb31f8eb563dacf0833'
BASE_BIT_SHA256 = '3239e9cc37ee4993c25f02ae6c97da0b3372f0e722e267d10073e20adc5d6717'
BASE_HWH_SHA256 = '222898ef2c0ccc4a0c47608e99d8dc7c9c6cdfed5130964884ff35005827ef3a'
FRAMES_PER_DESCRIPTOR = 4096
SLOT_COUNT = 64
PCM_RATE_HZ = 48000
PATTERN_WRAP_FRAMES = 1 << 20


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def verify_loaded(path, expected_sha256, state_file, fpga_state, fpga_flags):
    state = json.loads(state_file.read_text(encoding='utf-8'))
    requested = Path(path).resolve()
    loaded = Path(state.get('bitfile_name', '')).resolve()
    digest = hashlib.sha1(Path(path).read_bytes()).hexdigest()
    manager_state = fpga_state.read_text(encoding='ascii').strip()
    manager_flags = fpga_flags.read_text(encoding='ascii').strip()
    if loaded != requested or state.get('bitfile_hash') != digest:
        raise RuntimeError('PYNQ global PL state does not match the requested BIT')
    if manager_state != 'operating' or manager_flags not in ('0', '0x0'):
        raise RuntimeError('FPGA manager is not operating cleanly')
    if sha256(path) != expected_sha256:
        raise RuntimeError('Loaded BIT SHA-256 differs from the pinned artifact')
    return {
        'bitfile_name': str(loaded),
        'bitfile_hash_sha1': digest,
        'bitfile_sha256': expected_sha256,
        'pynq_active_name': state.get('active_name'),
        'fpga_manager_state': manager_state,
        'fpga_manager_flags': manager_flags,
    }


def request_snapshot(control, timeout=2.0):
    control.write(0x78, 0)
    control.write(0x78, 1)
    deadline = time.monotonic() + timeout
    while not control.read(0x7C) & 1:
        if time.monotonic() >= deadline:
            raise TimeoutError('Overflow diagnostic snapshot did not become valid')
        time.sleep(0.001)
    values = {
        'frames': (control.read(0x6C) << 32) | control.read(0x68),
        'words': (control.read(0x74) << 32) | control.read(0x70),
        'error_flags': control.read(0x2C),
        'overflow_count': control.read(0x54),
        'capture_status': control.read(0x28),
    }
    control.write(0x78, 0)
    return values


def inspect_retained_dma_buffers():
    """After PL reprogramming, inspect and release buffers retained on DMA reset failure."""
    from pynq_acoustic_camera.continuous_capture import _UNSAFE_CONTINUOUS_BUFFERS

    roots = list(_UNSAFE_CONTINUOUS_BUFFERS)
    result = []
    for index, buffer in enumerate(roots):
        buffer.invalidate()
        item = {
            'retained_index': index,
            'shape': list(buffer.shape),
            'dtype': str(buffer.dtype),
            'physical_address': int(buffer.physical_address),
        }
        if len(buffer.shape) == 2 and buffer.shape[1] == 16:
            item['first_descriptors_words_0_to_7'] = [
                [int(word) for word in row[:8]] for row in buffer[:2]]
        result.append(item)
    # The caller invokes this only after the official Base bitstream has been
    # reloaded and verified, so the previous S2MM engine cannot access CMA.
    for buffer in roots:
        buffer.freebuffer()
    _UNSAFE_CONTINUOUS_BUFFERS[:] = [
        buffer for buffer in _UNSAFE_CONTINUOUS_BUFFERS
        if all(buffer is not root for root in roots)]
    return result


def read_fault_registers(capture):
    """Snapshot the live DMA pointers and PCM counters before restoring Base."""
    dma = capture._dma
    control = capture._control
    registers = {
        'dma': {
            'control': dma.read(0x30),
            'status': dma.read(0x34),
            'current_descriptor': dma.read(0x38),
            'current_descriptor_msb': dma.read(0x3C),
            'tail_descriptor': dma.read(0x40),
            'tail_descriptor_msb': dma.read(0x44),
        },
        'capture': {
            'status': control.read(0x28),
            'error_flags': control.read(0x2C),
            'format': control.read(0x30),
            'source_pattern': control.read(0x34),
            'frames_produced': control.read(0x4C),
            'words_sent': control.read(0x50),
            'overflow_count': control.read(0x54),
            'total_frames_low': control.read(0x68),
            'total_frames_high': control.read(0x6C),
            'total_words_low': control.read(0x70),
            'total_words_high': control.read(0x74),
        },
    }
    root = capture._descriptor_root
    if root is not None:
        root.invalidate()
        registers['descriptors'] = [
            [int(word) for word in row[:8]] for row in root[:4]]
    return registers


def run_continuous_pattern(overlay, ContinuousPcmCapture, args, report):
    capture = ContinuousPcmCapture.from_overlay(
        overlay, provenance={
            'phase_step': '7.5',
            'source': 'synthetic_pattern',
            'candidate_build_id': BUILD_ID,
            'hardware_commit': args.hardware_commit,
            'software_commit': args.software_commit,
        })
    identity = capture.clock.identity
    if identity.build_id != BUILD_ID:
        raise RuntimeError('Mapped control IP BUILD_ID does not match this run')
    capture.enable_clock(timeout=2.0)
    target_packets = math.ceil(args.duration_seconds * PCM_RATE_HZ / FRAMES_PER_DESCRIPTOR)
    expected = capture.start(
        source='pattern', frames_per_descriptor=FRAMES_PER_DESCRIPTOR,
        slot_count=SLOT_COUNT, timeout=5.0)
    run = {
        'status': 'running',
        'started_utc': utc_now(),
        'requested_duration_seconds': args.duration_seconds,
        'target_packets': target_packets,
        'start': expected,
        'identity': {
            'magic': identity.magic,
            'abi_version': identity.abi_version,
            'capabilities': identity.capabilities,
            'channels': identity.channels,
            'build_id': identity.build_id,
        },
        'packets': [],
        'health_after_start': capture._diagnostics(),
    }
    report['continuous_pattern'] = run
    save_json(args.output / 'status.json', report)
    started = time.monotonic()
    try:
        for index in range(target_packets):
            packet = capture.read_packet(timeout=3.0)
            metadata = packet.metadata
            expected_first_frame = index * FRAMES_PER_DESCRIPTOR
            if metadata['packet_index'] != index:
                raise RuntimeError('Packet index sequence has a gap or duplicate')
            if metadata['first_frame'] != expected_first_frame:
                raise RuntimeError('First-frame counter is discontinuous at a buffer boundary')
            if metadata['build_id'] != BUILD_ID:
                raise RuntimeError('Packet identity changed during capture')
            run['packets'].append({
                'packet_index': index,
                'first_frame': metadata['first_frame'],
                'frames': metadata['frames'],
                'bytes': metadata['bytes'],
                'descriptor_status': metadata['descriptor_status'],
                'counter_frames': metadata['counters']['frames'],
                'counter_words': metadata['counters']['words'],
                'slot_index': index % SLOT_COUNT,
            })
            if (index + 1) % 64 == 0:
                print('PATTERN_PROGRESS packets={}'.format(index + 1), flush=True)

        elapsed = time.monotonic() - started
        result = capture.stop(timeout=8.0).metadata
        min_frames = target_packets * FRAMES_PER_DESCRIPTOR
        if result['packets_received'] < target_packets:
            raise RuntimeError('Capture stopped before the requested packet count')
        if result['frames_total'] != result['packets_received'] * FRAMES_PER_DESCRIPTOR:
            raise RuntimeError('Frame total does not span the complete descriptor sequence')
        if result['words_total'] * 4 != result['bytes_received']:
            raise RuntimeError('AXI word count differs from descriptor byte total')
        if result['frames_total'] * 16 != result['bytes_received']:
            raise RuntimeError('Frame total differs from descriptor byte total')
        if result['frames_total'] < min_frames or result['frames_total'] <= PATTERN_WRAP_FRAMES:
            raise RuntimeError('Run did not span its duration and the 20-bit pattern wrap')
        if result['overflows'] != 0 or result['clips'] != 0:
            raise RuntimeError('Unexpected overflow or clip in the normal pattern run')
        if result['cyclic_dma_mode']:
            raise RuntimeError('DMA cyclic mode was unexpectedly enabled')
        run.update({
            'status': 'pass',
            'finished_utc': utc_now(),
            'elapsed_seconds': elapsed,
            'packets_per_second': len(run['packets']) / elapsed,
            'ring_wraps_completed': result['packets_received'] // SLOT_COUNT,
            'pattern_20bit_wrap_crossed': True,
            'result': result,
        })
        print('PATTERN_PASS packets={} frames={} elapsed={:.3f}s'.format(
            result['packets_received'], result['frames_total'], elapsed), flush=True)
    except BaseException as error:
        run.update({'status': 'fail', 'finished_utc': utc_now(),
                    'error': '{}: {}'.format(type(error).__name__, error),
                    'traceback': traceback.format_exc()})
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
        if capture._state == 'idle' and capture.clock.clock_enabled:
            try:
                capture.disable_clock()
            except Exception as clock_error:
                run['clock_disable_error'] = str(clock_error)
        raise
    finally:
        if capture._state == 'stopped' and capture.clock.clock_enabled:
            capture.disable_clock()
        save_json(args.output / 'status.json', report)


def run_overload_check(overlay, ContinuousPcmCapture, args, report):
    capture = ContinuousPcmCapture.from_overlay(
        overlay, provenance={'phase_step': '7.5', 'source': 'controlled_no_slot_return'})
    capture.enable_clock(timeout=2.0)
    start_info = capture.start(
        source='pattern', frames_per_descriptor=FRAMES_PER_DESCRIPTOR,
        slot_count=2, timeout=5.0)
    result = {
        'status': 'running',
        'started_utc': utc_now(),
        'frames_per_descriptor': FRAMES_PER_DESCRIPTOR,
        'slot_count': 2,
        'queue_seconds_at_48khz': start_info['queue_seconds_at_48khz'],
        'consumer_stall_seconds': args.overload_stall_seconds,
    }
    report['controlled_overload'] = result
    save_json(args.output / 'status.json', report)
    try:
        # Leave both descriptors owned by DMA. The 2-slot queue then exhausts;
        # the diagnostic snapshot makes the hardware FIFO overflow visible.
        time.sleep(args.overload_stall_seconds)
        snapshot = request_snapshot(overlay.acoustic_capture_0.mmio)
        deadline = time.monotonic() + 2.0
        while not (snapshot['overflow_count'] or snapshot['error_flags'] & 0x2):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
            snapshot = request_snapshot(overlay.acoustic_capture_0.mmio)
        result['overflow_snapshot'] = snapshot
        if not (snapshot['overflow_count'] or snapshot['error_flags'] & 0x2):
            raise RuntimeError('The forced no-slot condition did not report FIFO overflow')
        try:
            capture.read_packet(timeout=1.0)
        except Exception as error:
            from pynq_acoustic_camera.continuous_capture import ContinuousCaptureError
            if not isinstance(error, ContinuousCaptureError):
                raise
            result['software_error'] = str(error)
            result['software_diagnostics'] = error.diagnostics
            if not (error.diagnostics.get('overflow_count')
                    or error.diagnostics.get('error_flags', 0) & 0x2):
                raise RuntimeError('Driver error omitted the hardware overflow indication') from error
            if error.diagnostics.get('recovery') != 'ok':
                raise RuntimeError('Driver did not recover DMA after the controlled overload') from error
        else:
            raise RuntimeError('Driver returned a packet instead of reporting the overload')
        result.update({'status': 'pass', 'finished_utc': utc_now(),
                       'recovery': 'ok', 'error_latch_cleared': capture._control.read(0x2C) == 0,
                       'overflow_latch_cleared': capture._control.read(0x54) == 0})
        if not result['error_latch_cleared'] or not result['overflow_latch_cleared']:
            raise RuntimeError('CLEAR left an error or overflow latched')
        print('OVERLOAD_PASS detected_and_recovered', flush=True)
    except BaseException as error:
        result.update({'status': 'fail', 'finished_utc': utc_now(),
                       'error': '{}: {}'.format(type(error).__name__, error),
                       'traceback': traceback.format_exc()})
        if capture._state in ('starting', 'running', 'stopping', 'faulted'):
            try:
                capture.recover()
                result['recovery'] = 'ok'
            except Exception as recovery_error:
                result['recovery'] = '{}: {}'.format(type(recovery_error).__name__, recovery_error)
        raise
    finally:
        if capture._state == 'idle' and capture.clock.clock_enabled:
            capture.disable_clock()
        save_json(args.output / 'status.json', report)


def run_descriptor_probe(overlay, ContinuousPcmCapture, args, report):
    """Exercise short S2MM SG packets to isolate packet/descriptor handling."""
    capture = ContinuousPcmCapture.from_overlay(
        overlay, provenance={
            'phase_step': '7.5',
            'scope': 'diagnostic_short_packet_probe',
            'source': 'synthetic_pattern',
            'candidate_build_id': BUILD_ID,
            'hardware_commit': args.hardware_commit,
            'software_commit': args.software_commit,
        })
    capture.enable_clock(timeout=2.0)
    start = capture.start(
        source='pattern', frames_per_descriptor=args.probe_frames,
        slot_count=args.probe_slots, timeout=5.0)
    probe = {
        'scope': 'diagnostic_only_not_phase_acceptance',
        'status': 'running',
        'requested_packets': args.probe_packets,
        'frames_per_descriptor': args.probe_frames,
        'slot_count': args.probe_slots,
        'start': start,
        'packets': [],
    }
    report['descriptor_probe'] = probe
    save_json(args.output / 'status.json', report)
    try:
        for index in range(args.probe_packets):
            packet = capture.read_packet(timeout=2.0)
            metadata = packet.metadata
            if metadata['packet_index'] != index:
                raise RuntimeError('Probe descriptor sequence has a gap or duplicate')
            if metadata['first_frame'] != index * args.probe_frames:
                raise RuntimeError('Probe frame counter is discontinuous')
            probe['packets'].append({
                'packet_index': index,
                'first_frame': metadata['first_frame'],
                'frames': metadata['frames'],
                'bytes': metadata['bytes'],
                'descriptor_status': metadata['descriptor_status'],
                'counter_frames': metadata['counters']['frames'],
                'counter_words': metadata['counters']['words'],
            })
        result = capture.stop(timeout=5.0).metadata
        if result['packets_received'] < args.probe_packets:
            raise RuntimeError('Probe stopped before all descriptors completed')
        probe.update({'status': 'pass', 'result': result, 'finished_utc': utc_now()})
        print('SG_PROBE_PASS packets={} frames={}'.format(
            result['packets_received'], result['frames_total']), flush=True)
    except BaseException as error:
        probe.update({
            'status': 'fail',
            'error': '{}: {}'.format(type(error).__name__, error),
            'traceback': traceback.format_exc(),
            'finished_utc': utc_now(),
        })
        diagnostics = getattr(error, 'diagnostics', None)
        if diagnostics is not None:
            probe['error_diagnostics'] = diagnostics
        if capture._state in ('starting', 'running', 'stopping', 'faulted'):
            try:
                capture.recover()
                probe['recovery'] = 'ok'
            except Exception as recovery_error:
                probe['recovery'] = '{}: {}'.format(
                    type(recovery_error).__name__, recovery_error)
        raise
    finally:
        if capture._state == 'stopped' and capture.clock.clock_enabled:
            capture.disable_clock()
        save_json(args.output / 'status.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'src')
    parser.add_argument('--duration-seconds', type=int, default=30)
    parser.add_argument('--overload-stall-seconds', type=float, default=0.8)
    parser.add_argument('--probe-packets', type=int)
    parser.add_argument('--probe-frames', type=int, default=4)
    parser.add_argument('--probe-slots', type=int, default=4)
    parser.add_argument('--hardware-commit', default='536ee0284fa1c9df151142d840e9a55974c93d1d')
    parser.add_argument('--software-commit', default='2b6118f6d6777727c45cf32285b9b1cc5c4b07d5')
    args = parser.parse_args()
    args.bit = args.bit.resolve()
    args.output = args.output.resolve()
    args.source = args.source.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if not (args.source / 'pynq_acoustic_camera' / '__init__.py').is_file():
        raise FileNotFoundError('Staged pynq_acoustic_camera source package is missing')
    # Prefer the phase-7 driver staged beside this runner over an older package
    # installed in the PYNQ image.
    sys.path.insert(0, str(args.source))

    report = {
        'phase': 7,
        'step': '7.5',
        'scope': 'diagnostic_only' if args.probe_packets is not None else 'phase_acceptance',
        'status': 'running',
        'started_utc': utc_now(),
        'board_programming': 'candidate_replaces_active_overlay_then_official_base_restored',
        'microphones_required': False,
        'microphones_used': False,
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
    status_path = args.output / 'status.json'
    save_json(status_path, report)
    shutil.copy2(Path(__file__), args.output / 'runner.py')
    base_restored = False
    candidate_load_attempted = False

    try:
        if os.geteuid() != 0:
            raise RuntimeError('Run this board verification as root')
        if sys.prefix != '/usr/local/share/pynq-venv':
            raise RuntimeError('Use the PYNQ virtualenv Python')
        if os.environ.get('XILINX_XRT') != '/usr':
            raise RuntimeError('XILINX_XRT must be /usr')
        if args.probe_packets is not None and args.probe_packets < 1:
            raise ValueError('Probe packet count must be positive')
        if args.probe_packets is not None and not 1 <= args.probe_frames <= 65536:
            raise ValueError('Probe frames must be in 1..65536')
        if args.probe_packets is not None and not 2 <= args.probe_slots <= 256:
            raise ValueError('Probe slots must be in 2..256')
        if args.probe_packets is None and args.duration_seconds < 30:
            raise ValueError('Use at least 30 seconds to wrap the descriptor ring and pattern counter')
        if report['candidate_bit_sha256'] != BIT_SHA256 or report['candidate_hwh_sha256'] != HWH_SHA256:
            raise RuntimeError('Candidate BIT/HWH does not match the reviewed manifest')

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
        if args.probe_packets is not None:
            run_descriptor_probe(overlay, ContinuousPcmCapture, args, report)
            report['status'] = 'probe_pass'
        else:
            run_continuous_pattern(overlay, ContinuousPcmCapture, args, report)
            run_overload_check(overlay, ContinuousPcmCapture, args, report)
            report['status'] = 'pass'
    except BaseException as error:
        report.update({'status': 'fail',
                       'error': '{}: {}'.format(type(error).__name__, error),
                       'traceback': traceback.format_exc()})
        print('STEP7_5_ERROR=' + report['error'], flush=True)
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
    result_name = 'STEP7_5_PROBE_RESULT' if args.probe_packets is not None else 'STEP7_5_RESULT'
    print(result_name + '=' + report['status'], flush=True)
    return 0 if report['status'] in ('pass', 'probe_pass') and base_restored else 1


if __name__ == '__main__':
    raise SystemExit(main())
