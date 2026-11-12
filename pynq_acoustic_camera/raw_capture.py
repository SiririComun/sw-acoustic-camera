"""Finite four-channel RAW capture with bounded AXI DMA S2MM polling.

No PYNQ imports or hardware writes on import/construction. Only call from_overlay
with an already loaded, hash-verified BIT/HWH pair. No PCM conversion is provided.
One caller owns the capture IP and DMA exclusively for this object's lifetime.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time

from .pdm_control import PdmClockControl, PdmIdentityError

# Retain allocations if a failed DMA reset cannot prove memory writes stopped.
_UNSAFE_BUFFERS = []


class RawCaptureError(RuntimeError):
    """No valid data is returned; diagnostics record the failure and recovery."""
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class RawCaptureResult:
    samples: object
    metadata: dict


class AcousticArray:
    """RAW-only API. The DMA MMIO must belong to a simple 32-bit S2MM engine.

    Constructor injection supports host models. On PYNQ prefer from_overlay,
    which verifies the relevant DMA parameters in the loaded HWH before writes.
    All waits have deadlines; no PYNQ DMA wait/stop method is used.
    """
    MAX_FRAMES = 65536
    DMA_CR, DMA_SR, DMA_DA, DMA_DA_MSB, DMA_LENGTH = 0x30, 0x34, 0x48, 0x4C, 0x58
    DMA_ERRORS = 0x70

    def __init__(self, control_mmio, dma_mmio, expected_build_id, *, allocator=None,
                 provenance=None, recovery_timeout=1.0):
        self.clock = PdmClockControl(control_mmio, expected_build_id)
        if self.clock.identity.capabilities != 0xB:
            raise PdmIdentityError('Expected CLOCK/RAW/PATTERN capabilities, without PCM')
        if control_mmio.read(0x3C) != self.MAX_FRAMES or self.clock.pdm_clock_hz != 2400000:
            raise PdmIdentityError('Unexpected RAW block limit or nominal clock')
        if not callable(getattr(dma_mmio, 'read', None)) or not callable(getattr(dma_mmio, 'write', None)):
            raise TypeError('dma_mmio must provide read and write')
        self._positive_timeout(recovery_timeout)
        self._control, self._dma = control_mmio, dma_mmio
        self._allocator = allocator
        self._provenance = dict(provenance or {})
        self._recovery_timeout = recovery_timeout
        self._faulted = False

    @classmethod
    def from_overlay(cls, overlay, expected_build_id, *, provenance=None):
        """Attach to a loaded and verified overlay; does not program the FPGA."""
        ips = overlay.ip_dict
        control, dma = ips['acoustic_capture_0'], ips['axi_dma_audio']
        params = dma['parameters']
        required = {'c_include_sg': 0, 'c_include_mm2s': 0, 'c_include_s2mm': 1,
                    'c_sg_length_width': 23, 'c_addr_width': 32,
                    'c_m_axi_s2mm_data_width': 32, 'c_s_axis_s2mm_tdata_width': 32}
        params = {key.lower(): value for key, value in params.items()}
        for key, expected in required.items():
            if int(str(params[key]), 0) != expected:
                raise PdmIdentityError('Unexpected DMA parameter: ' + key)
        ps = ips['processing_system7_0']['parameters']
        ps = {key.lower(): value for key, value in ps.items()}
        if int(str(ps['c_s_axi_hp0_data_width']), 0) != 64:
            raise PdmIdentityError('PYNQ image requires HP0 configured for 64-bit data')
        if not overlay.is_loaded():
            raise PdmIdentityError('Overlay is not loaded')
        from pynq import MMIO, allocate
        # Access MMIO directly: instantiating PYNQ's DMA driver can start it.
        return cls(MMIO(control['phys_addr'], control['addr_range']),
                   MMIO(dma['phys_addr'], dma['addr_range']), expected_build_id,
                   allocator=allocate, provenance=provenance)

    @staticmethod
    def _positive_timeout(value):
        if (isinstance(value, bool) or not isinstance(value, (float, int))
                or not math.isfinite(value) or value <= 0):
            raise ValueError('timeout must be finite and positive')

    def _wait(self, predicate, timeout, message):
        deadline = time.monotonic() + timeout
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(message)
            time.sleep(min(0.001, remaining))

    def _idle(self):
        if self._faulted:
            raise RawCaptureError('DMA recovery failed; reload a verified overlay before reuse', {})
        if self._control.read(0x28) & 2:
            raise RawCaptureError('Capture IP is busy', self._diagnostics())

    def enable_clock(self):
        self._idle()
        self.clock.enable_clock()
        self.clock.wait_clock_ready()

    def disable_clock(self):
        self._idle()
        self.clock.disable_clock()

    def _clear(self):
        self._control.write(0x24, 4)
        self._wait(lambda: not self._control.read(0x28) & 2,
                   self._recovery_timeout, 'Capture CLEAR did not finish')
        d = self._diagnostics()
        if d['status'] & 0xE or any(d[key] for key in ('error_flags', 'frames', 'words', 'overflows')):
            raise RuntimeError('CLEAR failed to clear capture status/counters')

    def _diagnostics(self):
        c = self._control
        return {'status': c.read(0x28), 'error_flags': c.read(0x2C),
                'frames': c.read(0x4C), 'words': c.read(0x50), 'overflows': c.read(0x54),
                'dma_status': self._dma.read(self.DMA_SR),
                'dma_bytes': self._dma.read(self.DMA_LENGTH)}

    def _recover(self):
        # ABORT preserves an offered word until DMA asserts its primary reset.
        # DMA reset is a bounded direct MMIO operation; do not call stop().
        self._control.write(0x24, 2)
        self._dma.write(self.DMA_CR, 4)
        self._wait(lambda: not self._dma.read(self.DMA_CR) & 4
                   and bool(self._dma.read(self.DMA_SR) & 1)
                   and not self._control.read(0x28) & 2,
                   self._recovery_timeout, 'DMA/capture local reset did not finish')
        self._clear()

    def capture(self, n_frames=4096, format='raw_pdm', source='microphones', *, timeout=None):
        """Return a detached uint32 (N,) array only after full block validation.

        The default deadline is max(2 s, 10*N/2.4e6). An explicit deadline may
        shorten a diagnostic run. Failure records pre-recovery diagnostics and
        resets the local stream; partial data is never returned.
        """
        if type(n_frames) is not int or not 1 <= n_frames <= self.MAX_FRAMES:
            raise ValueError('n_frames must be an integer in 1..65536')
        if format != 'raw_pdm' or source not in ('microphones', 'pattern'):
            raise ValueError('Only raw_pdm and microphones/pattern are available')
        deadline_seconds = max(2.0, 10 * n_frames / self.clock.pdm_clock_hz) if timeout is None else timeout
        self._positive_timeout(deadline_seconds)
        self._idle()
        if not self.clock.clock_ready:
            raise RawCaptureError('Enable the PDM clock and await readiness first', self._diagnostics())
        if not self._dma.read(self.DMA_SR) & 3:
            raise RawCaptureError('DMA is already active; exclusive ownership required', self._diagnostics())
        if self._dma.read(self.DMA_SR) & self.DMA_ERRORS:
            raise RawCaptureError('DMA has stale errors; call recover() first', self._diagnostics())
        import numpy as np
        allocate = self._allocator
        if allocate is None:
            from pynq import allocate
        buffer = allocate(shape=(n_frames,), dtype=np.uint32)
        safe_to_free = True
        started = datetime.now(timezone.utc).isoformat()
        try:
            address = int(buffer.physical_address)
            if address % 4 or address < 0 or address + 4 * n_frames > 2**32:
                raise ValueError('DMA buffer must be aligned and fully addressable with 32 bits')
            self._clear()
            self._control.write(0x30, 0)
            self._control.write(0x34, int(source == 'pattern'))
            self._control.write(0x38, n_frames)
            if self._control.read(0x2C):
                raise RuntimeError('Capture configuration rejected')
            # Flush before handing ownership to DMA; invalidate after completion.
            buffer.flush()
            safe_to_free = False
            self._dma.write(self.DMA_CR, 1)
            self._wait(lambda: not self._dma.read(self.DMA_SR) & 1,
                       self._recovery_timeout, 'DMA did not leave HALTED')
            self._dma.write(self.DMA_DA, address)
            self._dma.write(self.DMA_DA_MSB, 0)
            self._dma.write(self.DMA_LENGTH, 4 * n_frames)
            self._control.write(0x24, 1)
            latest = {}
            def finished():
                latest.update(self._diagnostics())
                if latest['error_flags'] or latest['status'] & 8 or latest['dma_status'] & self.DMA_ERRORS:
                    raise RuntimeError('Capture or DMA reported an error')
                return latest['status'] & 6 == 4 and latest['dma_status'] & 2 != 0
            self._wait(finished, deadline_seconds, 'RAW capture timed out')
            if (latest['frames'] != n_frames or latest['words'] != n_frames
                    or latest['overflows'] or latest['dma_bytes'] != 4 * n_frames):
                raise RuntimeError('Incomplete RAW block: length or counters disagree')
            safe_to_free = True
            buffer.invalidate()
            samples = np.array(buffer, dtype=np.uint32, copy=True, subok=False)
            return RawCaptureResult(samples, {
                'format': format, 'source': source, 'channels': 4, 'n_frames': n_frames,
                'nominal_sample_rate_hz': self.clock.pdm_clock_hz,
                'dtype': 'uint32', 'shape': [n_frames], 'bytes': 4 * n_frames,
                'timeout_seconds': deadline_seconds, 'counters': dict(latest),
                'build_id': self.clock.identity.build_id,
                'host_execution_start_utc': started,
                'host_execution_end_utc': datetime.now(timezone.utc).isoformat(),
                'provenance': dict(self._provenance)})
        except BaseException as error:
            diagnostics = self._diagnostics()
            diagnostics['cause'] = '{}: {}'.format(type(error).__name__, error)
            try:
                self._recover()
                safe_to_free = True
                diagnostics['recovery'] = 'ok'
            except Exception as recovery_error:
                self._faulted = True
                diagnostics['recovery'] = str(recovery_error)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise RawCaptureError('RAW capture failed: ' + str(error), diagnostics) from error
        finally:
            if safe_to_free:
                buffer.freebuffer()
            else:
                _UNSAFE_BUFFERS.append(buffer)

    def recover(self):
        """Explicitly abort/reset/clear the exclusively owned capture path."""
        if self._faulted:
            self._idle()
        try:
            self._recover()
        except Exception as error:
            self._faulted = True
            raise RawCaptureError('RAW recovery failed: ' + str(error), self._diagnostics()) from error
