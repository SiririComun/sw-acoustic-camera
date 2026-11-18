#!/usr/bin/env python3
"""Host-only checks for the phase-7.3 Scatter/Gather PCM driver."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pynq_acoustic_camera import (ContinuousCaptureError, ContinuousPcmCapture,
                                  ContinuousPcmPacket, ContinuousCaptureResult)
from pynq_acoustic_camera.continuous_capture import _UNSAFE_CONTINUOUS_BUFFERS
from pynq_acoustic_camera.pdm_control import PdmIdentityError

BUILD = ContinuousPcmCapture.BUILD_ID


class Buffer(np.ndarray):
    def __new__(cls, shape, dtype, address, owner):
        obj = np.zeros(shape, dtype=dtype).view(cls)
        obj._base_address = address
        obj._root_virtual_address = obj.ctypes.data
        obj.physical_address = address
        obj._owner = owner
        obj.freed = False
        return obj

    def __array_finalize__(self, source):
        if source is None:
            return
        base = getattr(source, '_base_address', None)
        root_virtual = getattr(source, '_root_virtual_address', None)
        self._base_address = base
        self._root_virtual_address = root_virtual
        self._owner = getattr(source, '_owner', None)
        self.physical_address = (base + self.ctypes.data - root_virtual
                                 if base is not None and root_virtual is not None else None)
        self.freed = getattr(source, 'freed', False)

    def flush(self):
        self._owner.cache_log.append(('flush', self.physical_address, self.nbytes))

    def invalidate(self):
        self._owner.cache_log.append(('invalidate', self.physical_address, self.nbytes))

    def freebuffer(self):
        self.freed = True


class Hardware:
    def __init__(self, *, reset_fails=False, bad_pattern=False, bad_pcm_range=False,
                 counter_mismatch=False):
        self.reset_fails = reset_fails
        self.bad_pattern = bad_pattern
        self.bad_pcm_range = bad_pcm_range
        self.counter_mismatch = counter_mismatch
        self.log, self.cache_log, self.buffers = [], [], []
        self.next_address = 0x10000000
        self.reg = {0: 0x4143414D, 4: ContinuousPcmCapture.ABI_VERSION,
                    8: ContinuousPcmCapture.CAPABILITIES, 12: 4,
                    0x20: 1, 0x28: 0x11, 0x3C: 65536,
                    0x40: 2400000, 0x44: 48000, 0x48: 50,
                    0x5C: 48000, 0x60: 6, 0x64: 0, 0x2C: 0,
                    0x4C: 0, 0x50: 0, 0x54: 0, 0x68: 0, 0x6C: 0,
                    0x70: 0, 0x74: 0, 0x7C: 0}
        for index in range(4):
            self.reg[0x10 + 4 * index] = (int(BUILD, 16) >> (32 * index)) & 0xFFFFFFFF
        self.dma = {0x30: 0, 0x34: 1, 0x38: 0, 0x3C: 0, 0x40: 0, 0x44: 0}
        self.started = False
        self.stop_requested = False
        self.scheduled = 0
        self.frames_total = 0
        self.words_total = 0
        self.descriptor_base = 0
        self.slot_count = 0
        self.frames_per_descriptor = 0
        self.sample_base = 0
        self.control = self._MMIO(self, False)
        self.engine = self._MMIO(self, True)

    class _MMIO:
        def __init__(self, owner, dma):
            self.owner, self.is_dma = owner, dma

        def read(self, offset):
            table = self.owner.dma if self.is_dma else self.owner.reg
            return table.get(offset, 0)

        def write(self, offset, value):
            self.owner.write(self.is_dma, offset, value)

    def allocate(self, shape, dtype):
        dtype = np.dtype(dtype)
        nbytes = int(np.prod(shape)) * dtype.itemsize
        address = (self.next_address + 4095) & ~4095
        self.next_address = address + ((nbytes + 4095) & ~4095)
        buffer = Buffer(shape, dtype, address, self)
        self.buffers.append(buffer)
        return buffer

    def _array_at(self, address, length, dtype, shape):
        dtype = np.dtype(dtype)
        for root in self.buffers:
            start, end = root.physical_address, root.physical_address + root.nbytes
            if start <= address and address + length <= end:
                byte_view = root.view(np.uint8).reshape(-1)
                offset = address - start
                return byte_view[offset:offset + length].view(dtype).reshape(shape)
        raise AssertionError('DMA address is outside allocated host buffers')

    def _schedule_one(self, slot):
        descriptor_address = self.descriptor_base + slot * 64
        descriptor = self._array_at(descriptor_address, 64, np.uint32, (16,))
        sample_address = int(descriptor[2]) | (int(descriptor[3]) << 32)
        packet_bytes = int(descriptor[6])
        assert packet_bytes == self.frames_per_descriptor * 16
        samples = self._array_at(sample_address, packet_bytes, np.int32,
                                 (self.frames_per_descriptor, 4))
        first = self.scheduled * self.frames_per_descriptor
        frames = (np.arange(self.frames_per_descriptor, dtype=np.uint64) + first) & 0xFFFFF
        encoded = (frames[:, None] << 4) | np.arange(4, dtype=np.uint64)[None, :]
        samples[:] = ((encoded ^ 0x800000) - 0x800000).astype(np.int32)
        if self.bad_pattern and self.scheduled == 1:
            samples[0, 2] += 1
        if self.bad_pcm_range and self.scheduled == 0:
            samples[0, 0] = 8388608
        descriptor[7] = (1 << 31) | (1 << 27) | (1 << 26) | packet_bytes
        self.scheduled += 1
        self.frames_total += self.frames_per_descriptor
        self.words_total += self.frames_per_descriptor * 4
        self.reg[0x4C] = self.frames_total & 0xFFFFFFFF
        self.reg[0x50] = self.words_total & 0xFFFFFFFF

    def _schedule_initial_queue(self):
        self.descriptor_base = self.dma[0x38] | (self.dma[0x3C] << 32)
        last = self.dma[0x40] | (self.dma[0x44] << 32)
        self.slot_count = (last - self.descriptor_base) // 64 + 1
        first_descriptor = self._array_at(self.descriptor_base, 64, np.uint32, (16,))
        self.frames_per_descriptor = int(first_descriptor[6]) // 16
        self.sample_base = int(first_descriptor[2]) | (int(first_descriptor[3]) << 32)
        for slot in range(self.slot_count):
            self._schedule_one(slot)
        self.dma[0x34] = 1 << 1  # SG engine paused at the tail pointer.

    def _extend_queue(self, tail_address):
        if not self.started or self.stop_requested:
            return
        slot = (tail_address - self.descriptor_base) // 64
        previous_slot = (self.scheduled - 1) % self.slot_count
        count = (slot - previous_slot) % self.slot_count
        for _ in range(count):
            self._schedule_one(self.scheduled % self.slot_count)
        self.dma[0x34] = 1 << 1

    def write(self, dma, offset, value):
        self.log.append(('dma' if dma else 'control', offset, value))
        if dma:
            if offset == 0x30:
                if value & 4:
                    if self.reset_fails:
                        self.dma[offset] = value
                    else:
                        self.dma.update({0x30: 0, 0x34: 1, 0x38: 0, 0x40: 0})
                elif value & 1:
                    self.dma.update({0x30: value, 0x34: 0})
                else:
                    self.dma.update({0x30: value, 0x34: 1})
            elif offset == 0x38:
                self.dma[offset] = value
            elif offset == 0x3C:
                self.dma[offset] = value
            elif offset == 0x40:
                self.dma[offset] = value
                self._extend_queue(value)
            elif offset == 0x44:
                self.dma[offset] = value
            elif offset == 0x34:
                self.dma[offset] &= ~value
            else:
                self.dma[offset] = value
            return

        if offset == 0x24:
            if value == ContinuousPcmCapture.CMD_CLEAR:
                self.reg.update({0x28: 0x11, 0x2C: 0, 0x4C: 0, 0x50: 0, 0x54: 0})
                self.frames_total = self.words_total = 0
                self.reg[0x7C] = 0
            elif value == ContinuousPcmCapture.CMD_START:
                self.started = True
                self.stop_requested = False
                self.scheduled = 0
                self.frames_total = self.words_total = 0
                self.reg.update({0x28: 0x13, 0x2C: 0, 0x4C: 0, 0x50: 0, 0x54: 0})
                self._schedule_initial_queue()
            elif value == ContinuousPcmCapture.CMD_STOP:
                self.stop_requested = True
                self.reg[0x28] = 0x15
                if self.counter_mismatch:
                    self.words_total -= 1
            elif value == ContinuousPcmCapture.CMD_ABORT:
                self.reg[0x28] = 0x19
                self.reg[0x2C] = 4
            return
        elif offset == ContinuousPcmCapture.REG_SNAPSHOT:
            if value == 0:
                self.reg[0x7C] = 0
            else:
                self.reg[0x68] = self.frames_total & 0xFFFFFFFF
                self.reg[0x6C] = (self.frames_total >> 32) & 0xFFFFFFFF
                self.reg[0x70] = self.words_total & 0xFFFFFFFF
                self.reg[0x74] = (self.words_total >> 32) & 0xFFFFFFFF
                self.reg[0x7C] = 1
            return
        elif offset == 0x20:
            self.reg[offset] = value
            self.reg[0x28] = (self.reg[0x28] | 1) if value else (self.reg[0x28] & ~1)
            return
        self.reg[offset] = value


class ContinuousPcmDriverChecks(unittest.TestCase):
    def driver(self, hardware, recovery_timeout=0.01):
        return ContinuousPcmCapture(hardware.control, hardware.engine, allocator=hardware.allocate,
                                    recovery_timeout=recovery_timeout,
                                    provenance={'test': 'host_model'})

    @staticmethod
    def overlay():
        class Overlay:
            ip_dict = {
                'acoustic_capture_0': {'phys_addr': 0x43C00000, 'addr_range': 65536},
                'axi_dma_audio': {'phys_addr': 0x40400000, 'addr_range': 65536,
                    'parameters': {
                        'c_include_sg': 1, 'c_include_mm2s': 0, 'c_include_s2mm': 1,
                        'c_sg_length_width': 23, 'c_addr_width': 32,
                        'c_m_axi_s2mm_data_width': 32, 'c_m_axi_s2mm_addr_width': 32,
                        'c_m_axi_sg_addr_width': 32, 'c_s_axis_s2mm_tdata_width': 32,
                        'c_include_s2mm_dre': 0, 'c_enable_multi_channel': 0,
                        'c_sg_include_stscntrl_strm': 0}},
                'processing_system7_0': {'parameters': {'C_S_AXI_HP0_DATA_WIDTH': 64}}}
            def is_loaded(self): return True
        return Overlay

    def test_construct_and_overlay_attach_are_read_only_and_check_hwh(self):
        hardware = Hardware()
        api = self.driver(hardware)
        self.assertEqual(hardware.log, [])
        overlay = self.overlay()
        with patch.dict(sys.modules, {'pynq': type('Pynq', (), {
                'MMIO': lambda address, length: hardware.control if address == 0x43C00000 else hardware.engine,
                'allocate': hardware.allocate})}):
            attached = ContinuousPcmCapture.from_overlay(overlay(), BUILD)
        self.assertEqual(hardware.log, [])
        overlay.ip_dict['axi_dma_audio']['parameters']['c_include_sg'] = 0
        with self.assertRaises(PdmIdentityError): ContinuousPcmCapture.from_overlay(overlay(), BUILD)
        self.assertEqual(hardware.log, [])
        self.assertIsInstance(api, ContinuousPcmCapture)
        self.assertIsInstance(attached, ContinuousPcmCapture)

    def test_packet_sequence_reuse_stop_counters_and_cache_ownership(self):
        hardware = Hardware()
        api = self.driver(hardware)
        prepared = api.start(source='pattern', frames_per_descriptor=17, slot_count=2)
        self.assertEqual(prepared['packet_bytes'], 17 * 16)
        self.assertEqual(prepared['queue_bytes'], 2 * 17 * 16)
        self.assertFalse(prepared['cyclic_dma_mode'])
        self.assertEqual(hardware.dma[0x30] & (1 << 4), 0)
        self.assertTrue(all(
            int(hardware._array_at(hardware.descriptor_base + i * 64, 64,
                                   np.uint32, (16,))[4]) == 0
            and int(hardware._array_at(hardware.descriptor_base + i * 64, 64,
                                       np.uint32, (16,))[5]) == 0
            for i in range(2)))
        arm = max(i for i, item in enumerate(hardware.log) if item == ('dma', 0x40, hardware.descriptor_base + 64))
        start = max(i for i, item in enumerate(hardware.log) if item == ('control', 0x24, 1))
        self.assertLess(arm, start)

        packets = [api.read_packet(timeout=0.1) for _ in range(3)]
        self.assertTrue(all(isinstance(packet, ContinuousPcmPacket) for packet in packets))
        self.assertEqual([packet.metadata['packet_index'] for packet in packets], [0, 1, 2])
        for packet in packets:
            first = packet.metadata['first_frame']
            encoded = ((np.arange(17, dtype=np.uint64) + first)[:, None] << 4) | np.arange(4, dtype=np.uint64)
            expected = ((encoded ^ 0x800000) - 0x800000).astype(np.int32)
            np.testing.assert_array_equal(packet.samples, expected)
            self.assertEqual(packet.samples.shape, (17, 4))
            self.assertEqual(packet.samples.dtype, np.int32)
            self.assertFalse(np.shares_memory(packet.samples, hardware.buffers[-1]))

        result = api.stop(timeout=0.1)
        stop_command = max(i for i, item in enumerate(hardware.log)
                           if item == ('control', 0x24, ContinuousPcmCapture.CMD_STOP))
        stop_reset = max(i for i, item in enumerate(hardware.log)
                         if item == ('dma', 0x30, ContinuousPcmCapture.DMA_RESET))
        self.assertGreater(stop_reset, stop_command)
        self.assertIsInstance(result, ContinuousCaptureResult)
        self.assertEqual(result.metadata['cyclic_dma_mode'], False)
        self.assertEqual(result.metadata['frames_total'], 5 * 17)
        self.assertEqual(result.metadata['words_total'], 5 * 17 * 4)
        self.assertEqual(result.metadata['bytes_received'], 5 * 17 * 16)
        self.assertEqual(result.metadata['packets_returned'], 3)
        self.assertEqual(result.metadata['packets_received'], 5)
        self.assertEqual(result.metadata['packets_drained_without_return'], 2)
        self.assertTrue(all(buffer.freed for buffer in hardware.buffers))
        self.assertTrue(any(item[0] == 'invalidate' and item[2] == 64 for item in hardware.cache_log))
        json.dumps(result.metadata, allow_nan=False)

    def test_default_queue_matches_phase7_architecture_without_mics(self):
        hardware = Hardware()
        api = self.driver(hardware)
        prepared = api.start()
        self.assertEqual(prepared['frames_per_descriptor'], 4096)
        self.assertEqual(prepared['slot_count'], 64)
        self.assertEqual(prepared['queue_bytes'], 4 * 1024 * 1024)
        self.assertAlmostEqual(prepared['queue_seconds_at_48khz'], 5.4613333333)
        result = api.stop(timeout=0.2)
        self.assertEqual(result.metadata['frames_total'], 64 * 4096)
        self.assertEqual(result.metadata['bytes_received'], 4 * 1024 * 1024)
        self.assertEqual(result.metadata['packets_drained_without_return'], 64)

    def test_active_dma_is_rejected_before_writes_or_allocations(self):
        hardware = Hardware()
        hardware.dma[0x34] = 0
        api = self.driver(hardware)
        with self.assertRaises(ContinuousCaptureError): api.start()
        self.assertEqual(hardware.log, [])
        self.assertEqual(hardware.buffers, [])

    def test_overflow_pattern_corruption_and_counter_mismatch_recover(self):
        for failure in ('overflow', 'pattern', 'counter'):
            with self.subTest(failure=failure):
                hardware = Hardware(bad_pattern=failure == 'pattern',
                                    counter_mismatch=failure == 'counter')
                api = self.driver(hardware)
                api.start(source='pattern', frames_per_descriptor=17, slot_count=2)
                if failure == 'overflow':
                    hardware.reg[0x54] = 1
                if failure == 'pattern':
                    # The corrupted second packet is encountered on the second read.
                    api.read_packet(timeout=0.1)
                with self.assertRaises(ContinuousCaptureError) as raised:
                    if failure == 'counter':
                        api.stop(timeout=0.1)
                    else:
                        api.read_packet(timeout=0.1)
                self.assertEqual(raised.exception.diagnostics['recovery'], 'ok')
                self.assertTrue(all(buffer.freed for buffer in hardware.buffers))

    def test_microphone_source_requires_filter_and_descriptor_error_is_recovered(self):
        hardware = Hardware()
        hardware.reg[0x28] = 1  # clock ready, filter not ready
        api = self.driver(hardware)
        with self.assertRaises(ContinuousCaptureError):
            api.start(source='microphones', frames_per_descriptor=17, slot_count=2, timeout=0.002)
        self.assertEqual(hardware.log, [])
        self.assertEqual(hardware.buffers, [])

        hardware.reg[0x28] = 0x11
        api.start(source='pattern', frames_per_descriptor=17, slot_count=2)
        descriptor = hardware._array_at(hardware.descriptor_base, 64, np.uint32, (16,))
        descriptor[7] |= 1 << 28
        with self.assertRaises(ContinuousCaptureError) as raised:
            api.read_packet(timeout=0.1)
        self.assertEqual(raised.exception.diagnostics['recovery'], 'ok')
        self.assertTrue(all(buffer.freed for buffer in hardware.buffers))

        hardware = Hardware(bad_pcm_range=True)
        api = self.driver(hardware)
        api.start(source='microphones', frames_per_descriptor=17, slot_count=2)
        with self.assertRaises(ContinuousCaptureError) as raised:
            api.read_packet(timeout=0.1)
        self.assertEqual(raised.exception.diagnostics['recovery'], 'ok')

    def test_failed_dma_reset_retains_buffers_and_refuses_reuse(self):
        hardware = Hardware()
        api = self.driver(hardware, recovery_timeout=0.002)
        api.start(source='pattern', frames_per_descriptor=17, slot_count=2)
        hardware.reset_fails = True
        hardware.reg[0x54] = 1
        with self.assertRaises(ContinuousCaptureError) as raised:
            api.read_packet(timeout=0.1)
        self.assertNotEqual(raised.exception.diagnostics['recovery'], 'ok')
        self.assertTrue(any(any(unsafe is buffer for unsafe in _UNSAFE_CONTINUOUS_BUFFERS)
                            for buffer in hardware.buffers))
        self.assertFalse(any(buffer.freed for buffer in hardware.buffers))
        writes = len(hardware.log)
        with self.assertRaises(ContinuousCaptureError): api.start()
        self.assertEqual(len(hardware.log), writes)

    def test_64_bit_snapshot_reads_high_words_and_validation_arguments(self):
        hardware = Hardware()
        api = self.driver(hardware)
        api._last_snapshot = {'frames': 0, 'words': 0}
        hardware.frames_total = (1 << 32) + 17
        hardware.words_total = (1 << 32) * 4 + 68
        snapshot = api._capture_snapshot()
        self.assertEqual(snapshot, {'frames': (1 << 32) + 17, 'words': (1 << 32) * 4 + 68})
        self.assertEqual(hardware.reg[0x7C], 0)
        for kwargs in ({'frames_per_descriptor': 0}, {'frames_per_descriptor': True},
                       {'slot_count': 1}, {'slot_count': 257}, {'source': 'invalid'}, {'timeout': 0}):
            with self.assertRaises(ValueError): api.start(**kwargs)
        self.assertEqual(hardware.log, [('control', 0x78, 0), ('control', 0x78, 1), ('control', 0x78, 0)])


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ContinuousPcmDriverChecks))
    if len(sys.argv) == 3 and sys.argv[1] == '--report':
        report_path = Path(sys.argv[2])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            'scope': 'host_models_only_not_board', 'tests': result.testsRun,
            'failures': len(result.failures), 'errors': len(result.errors),
            'result': 'pass' if result.wasSuccessful() else 'fail'}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
