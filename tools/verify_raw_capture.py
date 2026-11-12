#!/usr/bin/env python3
"""Host models of the phase-3.4 RAW driver; never imports PYNQ or accesses a board."""
import json
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pynq_acoustic_camera.raw_capture import AcousticArray, RawCaptureError, _UNSAFE_BUFFERS
from pynq_acoustic_camera.pdm_control import PdmIdentityError

BUILD = '9FD0DDCDB84D4C52910805339C28CE2E'


class Buffer(np.ndarray):
    def __new__(cls, shape, dtype):
        obj = np.zeros(shape, dtype=dtype).view(cls)
        obj.physical_address = 0x10000000
        obj.freed = obj.flushed = obj.invalidated = False
        return obj
    def freebuffer(self): self.freed = True
    def flush(self): self.flushed = True
    def invalidate(self): self.invalidated = True


class Hardware:
    def __init__(self, mode=None, reset_fails=False):
        self.mode, self.reset_fails = mode, reset_fails
        self.log, self.buffers = [], []
        self.reg = {0: 0x4143414D, 4: 0x10000, 8: 0xB, 12: 4,
                    0x28: 1, 0x3C: 65536, 0x40: 2400000}
        for i in range(4): self.reg[0x10+4*i] = (int(BUILD, 16) >> (32*i)) & 0xFFFFFFFF
        self.dma = {0x30: 0, 0x34: 1, 0x58: 0}
        parent = self
        class MMIO:
            def __init__(self, dma): self.is_dma = dma
            def read(self, offset): return (parent.dma if self.is_dma else parent.reg).get(offset, 0)
            def write(self, offset, value): parent.write(self.is_dma, offset, value)
        self.control, self.engine = MMIO(False), MMIO(True)
    def allocate(self, shape, dtype):
        b = Buffer(shape, dtype); self.buffers.append(b); return b
    def write(self, dma, offset, value):
        self.log.append(('dma' if dma else 'control', offset, value))
        if dma:
            self.dma[offset] = value
            if offset == 0x30:
                if value == 4 and not self.reset_fails:
                    self.dma.update({0x30: 0, 0x34: 1, 0x58: 0})
                    self.reg[0x28] &= ~2
                    self.mode = None
                elif value == 1:
                    self.dma[0x34] = 2
        elif offset == 0x24:
            if value == 4:
                self.reg.update({0x28: 1, 0x2C: 0, 0x4C: 0, 0x50: 0, 0x54: 0})
            elif value == 2:
                self.reg[0x2C] = 4
                self.reg[0x28] = self.reg.get(0x28, 1) | 8
            elif value == 1:
                n = self.reg[0x38]
                assert self.buffers[-1].flushed
                assert self.dma[0x58] == 4*n
                assert self.dma[0x30] == 1
                self.buffers[-1][:] = np.arange(n, dtype=np.uint32)
                self.reg.update({0x28: 5, 0x4C: n, 0x50: n})
                if self.mode == 'short': self.dma[0x58] -= 4
                if self.mode == 'count': self.reg[0x50] -= 1
                if self.mode == 'overflow': self.reg.update({0x28: 11, 0x2C: 2, 0x54: 1})
                if self.mode == 'dma': self.dma[0x34] = 0x10
                if self.mode == 'timeout': self.reg[0x28] = 3; self.dma[0x34] = 0
        else:
            self.reg[offset] = value


class RawDriverChecks(unittest.TestCase):
    def driver(self, h):
        return AcousticArray(h.control, h.engine, BUILD, allocator=h.allocate, recovery_timeout=0.005)

    def test_pattern_sizes_and_arm_before_start(self):
        h = Hardware(); a = self.driver(h)
        self.assertEqual(h.log, [])
        for n in (1, 2, 17, 4096, 65536):
            result = a.capture(n, source='pattern')
            np.testing.assert_array_equal(result.samples, np.arange(n, dtype=np.uint32))
            self.assertEqual(result.samples.shape, (n,))
            self.assertEqual(result.samples.dtype, np.uint32)
            self.assertEqual(result.metadata['bytes'], 4*n)
            self.assertEqual(result.metadata['timeout_seconds'], 2)
            self.assertTrue(h.buffers[-1].freed and h.buffers[-1].invalidated)
            self.assertFalse(np.shares_memory(result.samples, h.buffers[-1]))
            start = max(i for i,x in enumerate(h.log) if x == ('control', 0x24, 1))
            arm = max(i for i,x in enumerate(h.log) if x == ('dma', 0x58, 4*n))
            self.assertLess(arm, start)

    def test_identity_and_arguments_before_writes(self):
        h = Hardware(); h.reg[0] = 0
        with self.assertRaises(PdmIdentityError): self.driver(h)
        self.assertEqual(h.log, [])
        h = Hardware(); a = self.driver(h)
        for n in (0, -1, 65537, True, 1.5):
            with self.assertRaises(ValueError): a.capture(n)
        for kwargs in ({'format': 'pcm'}, {'source': 'wrong'}, {'timeout': float('nan')}, {'timeout': 0}):
            with self.assertRaises(ValueError): a.capture(**kwargs)
        self.assertEqual(h.log, [])
        self.assertEqual(h.buffers, [])

    def test_busy_and_not_ready_before_writes(self):
        for status in (3, 0):
            h = Hardware(); h.reg[0x28] = status; a = self.driver(h)
            with self.assertRaises(RawCaptureError): a.capture()
            self.assertEqual(h.log, [])

    def test_error_recovery_then_exact_capture(self):
        for mode in ('short', 'count', 'overflow', 'dma', 'timeout'):
            with self.subTest(mode=mode):
                h = Hardware(mode); a = self.driver(h)
                with self.assertRaises(RawCaptureError) as raised:
                    a.capture(17, source='pattern', timeout=0.004)
                self.assertEqual(raised.exception.diagnostics['recovery'], 'ok')
                self.assertTrue(h.buffers[-1].freed)
                result = a.capture(17, source='pattern')
                np.testing.assert_array_equal(result.samples, np.arange(17, dtype=np.uint32))

    def test_failed_reset_retains_dma_buffer_and_refuses_reuse(self):
        h = Hardware('timeout', reset_fails=True); a = self.driver(h)
        with self.assertRaises(RawCaptureError) as raised:
            a.capture(17, timeout=0.004)
        self.assertNotEqual(raised.exception.diagnostics['recovery'], 'ok')
        self.assertFalse(h.buffers[-1].freed)
        self.assertIs(_UNSAFE_BUFFERS[-1], h.buffers[-1])
        writes = len(h.log)
        with self.assertRaises(RawCaptureError): a.capture(17)
        self.assertEqual(writes, len(h.log))

    def test_explicit_abort_recovery_preserves_configuration(self):
        h = Hardware(); a = self.driver(h)
        h.reg.update({0x20: 1, 0x34: 1, 0x38: 17, 0x28: 3})
        a.recover()
        self.assertEqual([h.reg[x] for x in (0x20, 0x34, 0x38)], [1, 1, 17])
        np.testing.assert_array_equal(a.capture(2, source='pattern').samples, [0, 1])


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RawDriverChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if len(sys.argv) == 3 and sys.argv[1] == '--report':
        Path(sys.argv[2]).write_text(json.dumps({
            'scope': 'host_models_only_not_hardware', 'tests': result.testsRun,
            'failures': len(result.failures), 'errors': len(result.errors),
            'result': 'pass' if result.wasSuccessful() else 'fail'}, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
