"""Finite RAW/PCM capture; attaches to an already verified, loaded overlay.

The phase-3 driver remains available in raw_capture. This extension dispatches
0xB hardware to that implementation and adds the phase-4 0xF interface. Import
and construction never program hardware or write MMIO.
"""
from datetime import datetime, timezone
from importlib import resources
import json

from .pdm_control import PdmClockControl, PdmIdentityError
from .raw_capture import (
    AcousticArray as _RawArray,
    RawCaptureError as CaptureError,
    RawCaptureResult as CaptureResult,
    _UNSAFE_BUFFERS,
)


class AcousticArray(_RawArray):
    """Capture RAW or synchronous signed-24 PCM from the advertised hardware.

    Use from_overlay on PYNQ; the inherited factory checks DMA/HWH parameters
    and HP0=64 before importing PYNQ or constructing the injected MMIO objects.
    """

    def __init__(self, control_mmio, dma_mmio, expected_build_id, *, allocator=None,
                 provenance=None, recovery_timeout=1.0):
        clock = PdmClockControl(control_mmio, expected_build_id)
        self._pcm_capable = clock.identity.capabilities == 0xF
        self.pcm_rate_hz = 0
        self.decimation = 0
        self.filter_settle_frames = 0
        self._filter_design = None
        if clock.identity.capabilities == 0xB:
            super().__init__(control_mmio, dma_mmio, expected_build_id,
                             allocator=allocator, provenance=provenance,
                             recovery_timeout=recovery_timeout)
            return
        if not self._pcm_capable:
            raise PdmIdentityError('Expected CLOCK/RAW/PATTERN with optional PCM (0xB or 0xF)')
        expected = {0x3C: 65536, 0x40: 2400000, 0x44: 48000, 0x48: 50,
                    0x5C: 48000, 0x60: 6}
        for register, value in expected.items():
            if control_mmio.read(register) != value:
                raise PdmIdentityError('Unexpected PCM parameter at 0x{:02X}'.format(register))
        if not all(callable(getattr(dma_mmio, name, None)) for name in ('read', 'write')):
            raise TypeError('dma_mmio must provide read and write')
        self._positive_timeout(recovery_timeout)
        self.clock = clock
        self._control, self._dma = control_mmio, dma_mmio
        self._allocator = allocator
        self._provenance = dict(provenance or {})
        self._recovery_timeout = recovery_timeout
        self._faulted = False
        self.pcm_rate_hz, self.decimation, self.filter_settle_frames = 48000, 50, 6
        resource = resources.files('pynq_acoustic_camera.data').joinpath('pcm_filter_v1.json')
        self._filter_design = json.loads(resource.read_text(encoding='utf-8'))

    @property
    def filter_ready(self):
        status = self._control.read(0x28)
        return self._pcm_capable and status & 0x11 == 0x11

    def wait_filter_ready(self, timeout=1.0):
        """Wait read-only for physical PCM readiness; does not reset the filter."""
        self._positive_timeout(timeout)
        self._idle()
        if not self._pcm_capable:
            raise PdmIdentityError('Loaded hardware has no PCM filter')
        if not self.clock.clock_ready:
            raise CaptureError('Enable the clock and await CLOCK_READY first', self._diagnostics())
        def ready():
            if not self.clock.clock_ready:
                raise CaptureError('Clock readiness lost while awaiting filter', self._diagnostics())
            return self.filter_ready
        self._wait(ready, timeout, 'PCM filter did not become ready')

    def _diagnostics(self):
        result = super()._diagnostics()
        if self._pcm_capable:
            result.update(clips=self._control.read(0x58),
                          filter_ready=bool(result['status'] & 0x10),
                          format_register=self._control.read(0x30),
                          source_register=self._control.read(0x34))
        return result

    def _clear(self):
        super()._clear()
        if self._pcm_capable and self._control.read(0x58):
            raise RuntimeError('CLEAR failed to clear CLIP_COUNT')

    def capture(self, n_frames=4096, format='raw_pdm', source='microphones', *, timeout=None):
        """Return a detached (N,) uint32 RAW or (N,4) int32 PCM result.

        PCM format name is pcm_s24_in_s32. Hardware discards startup; software
        never removes another six samples or resets a settled filter per block.
        The capture wait defaults to max(2 s, 10*N/fs_nominal). Readiness/arming
        and recovery use separate finite waits. Pattern bypasses FILTER_READY.
        """
        if not self._pcm_capable:
            return super().capture(n_frames, format, source, timeout=timeout)
        if type(n_frames) is not int or not 1 <= n_frames <= self.MAX_FRAMES:
            raise ValueError('n_frames must be an integer in 1..65536')
        if format not in ('raw_pdm', 'pcm_s24_in_s32') or source not in ('microphones', 'pattern'):
            raise ValueError('Expected raw_pdm/pcm_s24_in_s32 and microphones/pattern')
        pcm = format == 'pcm_s24_in_s32'
        fs = self.pcm_rate_hz if pcm else self.clock.pdm_clock_hz
        deadline_seconds = max(2.0, 10*n_frames/fs) if timeout is None else timeout
        self._positive_timeout(deadline_seconds)
        self._idle()
        if not self.clock.clock_ready:
            raise CaptureError('Enable the clock and await readiness first', self._diagnostics())
        if not self._dma.read(self.DMA_SR) & 3:
            raise CaptureError('DMA is already active; exclusive ownership required', self._diagnostics())
        if self._dma.read(self.DMA_SR) & self.DMA_ERRORS:
            raise CaptureError('DMA has stale errors; call recover() first', self._diagnostics())
        if pcm and source == 'microphones':
            try:
                self.wait_filter_ready(timeout=min(deadline_seconds, self._recovery_timeout))
            except TimeoutError as error:
                diagnostics = self._diagnostics()
                diagnostics['cause'] = str(error)
                raise CaptureError('PCM filter is not ready', diagnostics) from error
        ready_at_start = self.filter_ready
        import numpy as np
        allocate = self._allocator
        if allocate is None:
            from pynq import allocate
        shape, dtype = ((n_frames, 4), np.int32) if pcm else ((n_frames,), np.uint32)
        byte_count = (16 if pcm else 4)*n_frames
        expected_words = (4 if pcm else 1)*n_frames
        buffer = allocate(shape=shape, dtype=dtype)
        safe_to_free, touched = True, False
        started = datetime.now(timezone.utc).isoformat()
        try:
            address = int(buffer.physical_address)
            if address % 4 or address < 0 or address + byte_count > 2**32:
                raise ValueError('DMA buffer must be aligned and fully 32-bit addressable')
            if buffer.shape != shape or buffer.dtype != dtype or not buffer.flags.c_contiguous or buffer.nbytes != byte_count:
                raise ValueError('Allocator returned an incompatible DMA buffer')
            touched = True
            self._clear()
            self._control.write(0x30, int(pcm))
            self._control.write(0x34, int(source == 'pattern'))
            self._control.write(0x38, n_frames)
            if self._control.read(0x2C):
                raise RuntimeError('Capture configuration rejected')
            buffer.flush()
            safe_to_free = False
            self._dma.write(self.DMA_CR, 1)
            self._wait(lambda: not self._dma.read(self.DMA_SR) & 1,
                       self._recovery_timeout, 'DMA did not leave HALTED')
            self._dma.write(self.DMA_DA, address)
            self._dma.write(self.DMA_DA_MSB, 0)
            self._dma.write(self.DMA_LENGTH, byte_count)
            self._control.write(0x24, 1)
            latest = {}
            def finished():
                latest.update(self._diagnostics())
                if latest['error_flags'] or latest['status'] & 8 or latest['dma_status'] & self.DMA_ERRORS:
                    raise RuntimeError('Capture or DMA reported an error')
                return latest['status'] & 6 == 4 and latest['dma_status'] & 2 != 0
            self._wait(finished, deadline_seconds, 'Acquisition timed out')
            if (latest['frames'] != n_frames or latest['words'] != expected_words
                    or latest['overflows'] or latest['dma_bytes'] != byte_count):
                raise RuntimeError('Incomplete block: bytes or counters disagree')
            clips = latest['clips']
            if not 0 <= clips <= 4*n_frames or ((not pcm or source == 'pattern') and clips):
                raise RuntimeError('CLIP_COUNT incompatible with block/source')
            safe_to_free = True
            buffer.invalidate()
            samples = np.array(buffer, dtype=dtype, copy=True, subok=False)
            if pcm and (np.any(samples < -8388608) or np.any(samples > 8388607)):
                raise RuntimeError('PCM signed-24 sign extension is invalid')
            return CaptureResult(samples, {
                'format': format, 'source': source, 'channels': 4, 'n_frames': n_frames,
                'nominal_sample_rate_hz': fs, 'pdm_clock_hz': self.clock.pdm_clock_hz,
                'decimation': self.decimation if pcm else 1,
                'dtype': np.dtype(dtype).name, 'shape': list(shape), 'bytes': byte_count,
                'pcm_normalization_divisor': 2**23 if pcm else None,
                'filter_applied': pcm and source == 'microphones',
                'filter_id': self._filter_design['filter_id'] if pcm else None,
                'filter_coefficients_sha256_le_i32': self._filter_design['fixed_point']['fir_coefficient_sha256_le_i32'] if pcm else None,
                'filter_settle_frames': self.filter_settle_frames if pcm else 0,
                'filter_ready_at_start': ready_at_start,
                'filter_reset_per_block': False,
                'timeout_seconds': deadline_seconds, 'counters': dict(latest),
                'build_id': self.clock.identity.build_id,
                'host_execution_start_utc': started,
                'host_execution_end_utc': datetime.now(timezone.utc).isoformat(),
                'provenance': dict(self._provenance)})
        except BaseException as error:
            diagnostics = self._diagnostics()
            diagnostics['cause'] = '{}: {}'.format(type(error).__name__, error)
            if touched:
                try:
                    self._recover()
                    safe_to_free = True
                    diagnostics['recovery'] = 'ok'
                except Exception as recovery_error:
                    self._faulted = True
                    diagnostics['recovery'] = str(recovery_error)
            else:
                diagnostics['recovery'] = 'not_needed_before_hardware_writes'
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CaptureError('Acquisition failed: ' + str(error), diagnostics) from error
        finally:
            if safe_to_free:
                buffer.freebuffer()
            else:
                _UNSAFE_BUFFERS.append(buffer)

    def recover(self):
        """Abort/reset/clear transport while retaining the physical filter state."""
        if self._faulted:
            self._idle()
        try:
            self._recover()
        except Exception as error:
            self._faulted = True
            raise CaptureError('Acquisition recovery failed: ' + str(error), self._diagnostics()) from error
