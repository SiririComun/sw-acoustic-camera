"""Continuous PCM capture using an explicitly owned AXI DMA S2MM BD queue.

PYNQ's high-level DMA class is not used: PYNQ 3.1 supports simple DMA mode,
whereas this candidate requires Scatter/Gather. Import and construction are
read-only. The cyclic-mode bit remains clear; software advances TAILDESC only
after it has consumed a completed slot.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time

from .pdm_control import PdmClockControl, PdmIdentityError
from .raw_capture import RawCaptureError


class ContinuousCaptureError(RawCaptureError):
    """Continuous capture failed; diagnostics are captured before recovery."""


@dataclass(frozen=True)
class ContinuousPcmPacket:
    """One detached PCM packet and its capture metadata."""

    samples: object
    metadata: dict


@dataclass(frozen=True)
class ContinuousCaptureResult:
    """Final counters and slot accounting for a stopped continuous capture."""

    metadata: dict


_UNSAFE_CONTINUOUS_BUFFERS = []


class ContinuousPcmCapture:
    """Own the S2MM descriptor ring for one continuous PCM capture.

    The default queue has 64 descriptors of 4096 four-channel frames each
    (4 MiB total, about 5.46 seconds at 48 kHz). ``read_packet`` must be called
    often enough to return slots before the queue fills. Exhaustion is reported
    as a capture error by the hardware and invalidates the session.

    ``from_overlay`` attaches to an already-loaded overlay and checks its HWH;
    it does not load or program a bitstream. For host models, inject MMIO-like
    objects and an allocator that returns PYNQ-compatible contiguous buffers.
    """

    BUILD_ID = '7A2C7F017A2C4E7D8F70070100000002'
    ABI_VERSION = 0x00010001
    CAPABILITIES = 0x0000001F
    PCM_RATE_HZ = 48000
    PDM_CLOCK_HZ = 2400000
    MAX_FRAMES = 65536
    DEFAULT_FRAMES_PER_DESCRIPTOR = 4096
    DEFAULT_SLOT_COUNT = 64
    MAX_SLOT_COUNT = 256

    REG_MODE = 0x64
    REG_TOTAL_FRAMES_LO = 0x68
    REG_TOTAL_FRAMES_HI = 0x6C
    REG_TOTAL_WORDS_LO = 0x70
    REG_TOTAL_WORDS_HI = 0x74
    REG_SNAPSHOT = 0x78
    REG_SNAPSHOT_VALID = 0x7C

    CMD_START = 1
    CMD_ABORT = 2
    CMD_CLEAR = 4
    CMD_STOP = 8
    STATUS_BUSY = 1 << 1
    STATUS_DONE = 1 << 2

    DMA_CR = 0x30
    DMA_SR = 0x34
    DMA_CURDESC = 0x38
    DMA_CURDESC_MSB = 0x3C
    DMA_TAILDESC = 0x40
    DMA_TAILDESC_MSB = 0x44
    DMA_RUN = 1 << 0
    DMA_RESET = 1 << 2
    DMA_CYCLIC = 1 << 4
    DMA_HALTED = 1 << 0
    DMA_IDLE = 1 << 1
    DMA_ERRORS = (0x7 << 4) | (0x7 << 8)
    DMA_W1C = DMA_ERRORS | (0x7 << 12)

    BD_BYTES = 64
    BD_NEXT_LO = 0
    BD_NEXT_HI = 1
    BD_BUFFER_LO = 2
    BD_BUFFER_HI = 3
    BD_CACHE_USER = 4
    BD_CONTROL = 6
    BD_STATUS = 7
    BD_RXSOF = 1 << 27
    BD_RXEOF = 1 << 26
    BD_INTERNAL_ERROR = (1 << 28) | (1 << 29) | (1 << 30)
    BD_COMPLETE = 1 << 31
    BD_LENGTH_MASK = (1 << 23) - 1
    BD_AWCACHE_NORMAL = 0x3 << 24

    def __init__(self, control_mmio, dma_mmio, expected_build_id=BUILD_ID, *,
                 allocator=None, recovery_timeout=1.0, provenance=None):
        clock = PdmClockControl(control_mmio, expected_build_id)
        if clock.identity.abi_version != self.ABI_VERSION:
            raise PdmIdentityError('Expected continuous PCM ABI 0x00010001')
        if clock.identity.capabilities != self.CAPABILITIES:
            raise PdmIdentityError('Expected CLOCK/RAW/PATTERN/PCM/CONTINUOUS capabilities (0x1F)')
        expected = {0x3C: self.MAX_FRAMES, 0x40: self.PDM_CLOCK_HZ,
                    0x44: self.PCM_RATE_HZ, 0x48: 50, 0x5C: 48000, 0x60: 6}
        for register, value in expected.items():
            if control_mmio.read(register) != value:
                raise PdmIdentityError('Unexpected continuous PCM parameter at 0x{:02X}'.format(register))
        if not callable(getattr(dma_mmio, 'read', None)) or not callable(getattr(dma_mmio, 'write', None)):
            raise TypeError('dma_mmio must provide read(offset) and write(offset, value)')
        self._positive_timeout(recovery_timeout)
        self.clock = clock
        self._control, self._dma = control_mmio, dma_mmio
        self._allocator = allocator
        self._recovery_timeout = recovery_timeout
        self._provenance = dict(provenance or {})
        self._state = 'idle'
        self._faulted = False
        self._source = None
        self._frames_per_descriptor = 0
        self._slot_count = 0
        self._descriptor_root = None
        self._sample_root = None
        self._retained_buffers = []
        self._descriptors = []
        self._sample_slots = []
        self._slot_states = []
        self._next_read_index = 0
        self._packets_received = 0
        self._packets_returned = 0
        self._packets_drained = 0
        self._bytes_received = 0
        self._last_snapshot = {'frames': 0, 'words': 0}
        self._started_utc = None
        self._last_result = None

    @classmethod
    def from_overlay(cls, overlay, expected_build_id=BUILD_ID, *, provenance=None):
        """Attach to the continuous candidate after read-only HWH checks."""
        ips = overlay.ip_dict
        control, dma = ips['acoustic_capture_0'], ips['axi_dma_audio']
        params = {key.lower(): value for key, value in dma['parameters'].items()}
        required = {'c_include_sg': 1, 'c_include_mm2s': 0, 'c_include_s2mm': 1,
                    'c_sg_length_width': 23, 'c_addr_width': 32,
                    'c_m_axi_s2mm_data_width': 32, 'c_m_axi_s2mm_addr_width': 32,
                    'c_m_axi_sg_addr_width': 32, 'c_s_axis_s2mm_tdata_width': 32,
                    'c_include_s2mm_dre': 0, 'c_enable_multi_channel': 0,
                    'c_sg_include_stscntrl_strm': 1}
        for key, expected in required.items():
            try:
                value = int(str(params[key]), 0)
            except (KeyError, ValueError) as error:
                raise PdmIdentityError('Missing or invalid DMA HWH parameter: ' + key) from error
            if value != expected:
                raise PdmIdentityError('Unexpected DMA HWH parameter: ' + key)
        ps = {key.lower(): value for key, value in ips['processing_system7_0']['parameters'].items()}
        try:
            hp0_width = int(str(ps['c_s_axi_hp0_data_width']), 0)
        except (KeyError, ValueError) as error:
            raise PdmIdentityError('Missing or invalid PS HP0 width in HWH') from error
        if hp0_width != 64:
            raise PdmIdentityError('PYNQ-Z2 candidate requires HP0 configured for 64-bit data')
        if not overlay.is_loaded():
            raise PdmIdentityError('Overlay is not loaded')
        from pynq import MMIO, allocate
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

    @property
    def filter_ready(self):
        return bool(self._control.read(0x28) & 0x11 == 0x11)

    def enable_clock(self, timeout=1.0):
        self._ensure_idle()
        self._positive_timeout(timeout)
        self.clock.enable_clock()
        self.clock.wait_clock_ready(timeout=timeout)

    def disable_clock(self):
        self._ensure_idle()
        self.clock.disable_clock()

    def wait_filter_ready(self, timeout=1.0):
        self._positive_timeout(timeout)
        self._ensure_idle()
        if not self.clock.clock_ready:
            raise ContinuousCaptureError('Enable the PDM clock and await CLOCK_READY first',
                                         self._diagnostics())
        self._wait(lambda: self.filter_ready, timeout, 'PCM filter did not become ready')

    def _ensure_idle(self):
        if self._faulted:
            raise ContinuousCaptureError('DMA recovery failed; reload a verified overlay before reuse', {})
        if self._state in ('starting', 'running', 'stopping'):
            raise ContinuousCaptureError('A continuous capture session already owns the DMA',
                                         self._diagnostics())

    def _diagnostics(self, *, include_sg=False):
        diagnostics = {
            'capture_status': self._control.read(0x28),
            'error_flags': self._control.read(0x2C),
            'overflow_count': self._control.read(0x54),
            'dma_control': self._dma.read(self.DMA_CR),
            'dma_status': self._dma.read(self.DMA_SR),
            'packets_received': self._packets_received,
            'packets_returned': self._packets_returned,
            'bytes_received': self._bytes_received,
            'last_snapshot': dict(self._last_snapshot),
        }
        if include_sg:
            try:
                diagnostics['capture_fault_snapshot'] = self._capture_snapshot(
                    timeout=min(0.2, self._recovery_timeout))
            except Exception as snapshot_error:
                diagnostics['capture_fault_snapshot_error'] = '{}: {}'.format(
                    type(snapshot_error).__name__, snapshot_error)
            diagnostics.update({
                'dma_current_descriptor': self._dma.read(self.DMA_CURDESC),
                'dma_current_descriptor_msb': self._dma.read(self.DMA_CURDESC_MSB),
                'dma_tail_descriptor': self._dma.read(self.DMA_TAILDESC),
                'dma_tail_descriptor_msb': self._dma.read(self.DMA_TAILDESC_MSB),
            })
            if self._descriptor_root is not None:
                self._descriptor_root.invalidate()
                diagnostics['dma_descriptor_statuses'] = [
                    int(descriptor[self.BD_STATUS]) for descriptor in self._descriptors]
                diagnostics['dma_descriptors_first_4'] = [
                    [int(word) for word in descriptor[:8]]
                    for descriptor in self._descriptors[:4]]
        return diagnostics

    def _check_health(self):
        diagnostics = self._diagnostics()
        if diagnostics['error_flags'] or diagnostics['capture_status'] & (1 << 3):
            raise RuntimeError('Capture IP reported ERROR_FLAGS')
        if diagnostics['overflow_count']:
            raise RuntimeError('Capture IP reported FIFO overflow')
        if diagnostics['dma_status'] & self.DMA_ERRORS:
            raise RuntimeError('AXI DMA Scatter/Gather reported an error')
        if self._state == 'running' and diagnostics['dma_status'] & self.DMA_HALTED:
            raise RuntimeError('AXI DMA halted before the capture STOP sequence')
        return diagnostics

    def _allocate(self, shape, dtype):
        import numpy as np

        allocator = self._allocator
        if allocator is None:
            from pynq import allocate
            allocator = allocate
        buffer = allocator(shape=shape, dtype=dtype)
        required = ('physical_address', 'nbytes', 'shape', 'dtype', 'flags',
                    'flush', 'invalidate', 'freebuffer')
        if any(not hasattr(buffer, name) for name in required):
            raise TypeError('Allocator must return a PYNQ-compatible contiguous buffer')
        expected_bytes = math.prod(shape) * np.dtype(dtype).itemsize
        if not buffer.flags.c_contiguous or buffer.nbytes != expected_bytes:
            raise ValueError('Allocator returned an incompatible DMA allocation')
        return buffer

    @staticmethod
    def _physical_address(buffer, byte_count, alignment, description):
        address = int(buffer.physical_address)
        if address < 0 or address % alignment or address + byte_count > 2**32:
            raise ValueError('{} must be aligned and fully 32-bit addressable'.format(description))
        return address

    def _reset_dma(self, timeout=None):
        timeout = self._recovery_timeout if timeout is None else timeout
        self._dma.write(self.DMA_CR, self.DMA_RESET)
        self._wait(lambda: not self._dma.read(self.DMA_CR) & self.DMA_RESET
                   and bool(self._dma.read(self.DMA_SR) & self.DMA_HALTED),
                   timeout, 'AXI DMA S2MM reset did not finish')
        status = self._dma.read(self.DMA_SR)
        if status & self.DMA_ERRORS:
            self._dma.write(self.DMA_SR, self.DMA_W1C)
            status = self._dma.read(self.DMA_SR)
        if status & self.DMA_ERRORS:
            raise RuntimeError('AXI DMA errors remained set after reset')

    def _clear_capture(self, timeout=None):
        timeout = self._recovery_timeout if timeout is None else timeout
        self._control.write(0x24, self.CMD_CLEAR)
        self._wait(lambda: not self._control.read(0x28) & self.STATUS_BUSY,
                   timeout, 'Capture CLEAR did not finish')
        if (self._control.read(0x2C) or self._control.read(0x4C)
                or self._control.read(0x50) or self._control.read(0x54)
                or self._control.read(0x58)):
            raise RuntimeError('Capture CLEAR failed to clear status and counters')

    def _prepare_slots(self, frames_per_descriptor, slot_count):
        import numpy as np

        packet_bytes = frames_per_descriptor * 16
        descriptor_root = self._allocate((slot_count, 16), np.uint32)
        self._descriptor_root = descriptor_root
        sample_root = self._allocate((slot_count, frames_per_descriptor, 4), np.int32)
        self._sample_root = sample_root
        descriptor_base = self._physical_address(descriptor_root, slot_count * self.BD_BYTES,
                                                 self.BD_BYTES, 'Descriptor array')
        sample_base = self._physical_address(sample_root, slot_count * packet_bytes, 4,
                                             'PCM sample pool')
        if descriptor_root.dtype != np.dtype(np.uint32) or sample_root.dtype != np.dtype(np.int32):
            raise ValueError('Allocator returned an incompatible DMA dtype')

        self._descriptors = [descriptor_root[index] for index in range(slot_count)]
        self._sample_slots = [sample_root[index] for index in range(slot_count)]
        self._slot_states = ['dma_owned'] * slot_count
        for index, descriptor in enumerate(self._descriptors):
            next_index = (index + 1) % slot_count
            next_address = descriptor_base + next_index * self.BD_BYTES
            sample_address = sample_base + index * packet_bytes
            if int(descriptor.physical_address) != descriptor_base + index * self.BD_BYTES:
                raise ValueError('Descriptor view does not map to a 64-byte BD slot')
            if int(self._sample_slots[index].physical_address) != sample_address:
                raise ValueError('PCM sample view does not map to its expected slot')
            descriptor[:] = 0
            descriptor[self.BD_NEXT_LO] = next_address & 0xFFFFFFFF
            descriptor[self.BD_NEXT_HI] = 0
            descriptor[self.BD_BUFFER_LO] = sample_address & 0xFFFFFFFF
            descriptor[self.BD_BUFFER_HI] = 0
            descriptor[self.BD_CACHE_USER] = self.BD_AWCACHE_NORMAL
            descriptor[self.BD_CONTROL] = packet_bytes
            self._sample_slots[index].invalidate()
        descriptor_root.flush()
        return descriptor_base, packet_bytes

    def start(self, *, source='pattern', frames_per_descriptor=DEFAULT_FRAMES_PER_DESCRIPTOR,
              slot_count=DEFAULT_SLOT_COUNT, timeout=5.0):
        """Prepare the SG queue, arm DMA, then start continuous PCM production."""
        self._ensure_idle()
        self._positive_timeout(timeout)
        if source not in ('pattern', 'microphones'):
            raise ValueError("source must be 'pattern' or 'microphones'")
        if type(frames_per_descriptor) is not int or not 1 <= frames_per_descriptor <= self.MAX_FRAMES:
            raise ValueError('frames_per_descriptor must be an integer in 1..65536')
        if type(slot_count) is not int or not 2 <= slot_count <= self.MAX_SLOT_COUNT:
            raise ValueError('slot_count must be an integer in 2..256')
        packet_bytes = frames_per_descriptor * 16
        if packet_bytes > self.BD_LENGTH_MASK:
            raise ValueError('PCM packet exceeds the configured DMA descriptor length width')
        if not self.clock.clock_ready:
            raise ContinuousCaptureError('Enable the PDM clock and await CLOCK_READY first',
                                         self._diagnostics())
        if source == 'microphones' and not self.filter_ready:
            try:
                self.wait_filter_ready(timeout=min(timeout, self._recovery_timeout))
            except TimeoutError as error:
                diagnostics = self._diagnostics()
                diagnostics['cause'] = str(error)
                raise ContinuousCaptureError('PCM filter is not ready', diagnostics) from error

        if self._control.read(0x28) & self.STATUS_BUSY:
            raise ContinuousCaptureError('Capture IP is already busy', self._diagnostics())
        if self._control.read(0x2C):
            raise ContinuousCaptureError('Capture IP has stale errors; recover first', self._diagnostics())
        if not self._dma.read(self.DMA_SR) & self.DMA_HALTED:
            raise ContinuousCaptureError('AXI DMA is active; exclusive ownership is required',
                                         self._diagnostics())

        self._frames_per_descriptor = frames_per_descriptor
        self._slot_count = slot_count
        self._source = source
        self._next_read_index = 0
        self._packets_received = self._packets_returned = self._packets_drained = 0
        self._bytes_received = 0
        self._last_snapshot = {'frames': 0, 'words': 0}
        self._started_utc = datetime.now(timezone.utc).isoformat()
        self._last_result = None
        self._state = 'starting'
        touched_hardware = False
        try:
            # The dedicated candidate is exclusively owned by this API. Reset
            # clears any old SG queue before its descriptor memory is replaced.
            touched_hardware = True
            self._reset_dma(timeout=min(timeout, self._recovery_timeout))
            self._clear_capture(timeout=min(timeout, self._recovery_timeout))
            self._control.write(0x30, 1)  # PCM_S24_IN_S32
            self._control.write(0x34, int(source == 'pattern'))
            self._control.write(0x38, frames_per_descriptor)
            self._control.write(self.REG_MODE, 1)  # continuous PCM mode
            if self._control.read(0x2C):
                raise RuntimeError('Continuous PCM configuration was rejected')

            descriptor_base, packet_bytes = self._prepare_slots(frames_per_descriptor, slot_count)
            self._dma.write(self.DMA_CURDESC_MSB, 0)
            self._dma.write(self.DMA_CURDESC, descriptor_base)
            self._dma.write(self.DMA_CR, self.DMA_RUN)  # RS=1; cyclic bit remains zero
            if self._dma.read(self.DMA_CR) & self.DMA_CYCLIC:
                raise RuntimeError('AXI DMA cyclic descriptor mode must remain disabled')
            self._wait(lambda: not self._dma.read(self.DMA_SR) & self.DMA_HALTED,
                       min(timeout, self._recovery_timeout), 'AXI DMA did not leave HALTED')
            last_descriptor = descriptor_base + (slot_count - 1) * self.BD_BYTES
            self._dma.write(self.DMA_TAILDESC_MSB, 0)
            self._dma.write(self.DMA_TAILDESC, last_descriptor)
            # S2MM is now ready and has the complete descriptor chain.
            self._control.write(0x24, self.CMD_START)
            if self._control.read(0x2C):
                raise RuntimeError('Continuous PCM START was rejected')
            self._state = 'running'
            return {'source': source, 'frames_per_descriptor': frames_per_descriptor,
                    'slot_count': slot_count, 'packet_bytes': packet_bytes,
                    'queue_bytes': slot_count * packet_bytes,
                    'queue_seconds_at_48khz': slot_count * frames_per_descriptor / self.PCM_RATE_HZ,
                    'cyclic_dma_mode': False, 'build_id': self.clock.identity.build_id}
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                if touched_hardware:
                    self._recover_interrupted()
                else:
                    self._release_buffers(safe=True)
                raise
            if touched_hardware:
                try:
                    self._recover_hardware()
                    self._release_buffers(safe=True)
                except Exception as recovery_error:
                    self._faulted = True
                    self._state = 'faulted'
                    self._release_buffers(safe=False)
                    details = {'recovery': str(recovery_error)}
                else:
                    self._state = 'idle'
                    details = {'recovery': 'ok'}
            else:
                self._release_buffers(safe=True)
                details = {'recovery': 'not_needed'}
                self._state = 'idle'
            diagnostics = self._diagnostics()
            diagnostics.update(details, cause='{}: {}'.format(type(error).__name__, error))
            raise ContinuousCaptureError('Could not start continuous PCM capture: ' + str(error),
                                         diagnostics) from error

    def _capture_snapshot(self, timeout=None):
        timeout = self._recovery_timeout if timeout is None else timeout
        self._control.write(self.REG_SNAPSHOT, 0)
        self._control.write(self.REG_SNAPSHOT, 1)
        self._wait(lambda: bool(self._control.read(self.REG_SNAPSHOT_VALID) & 1),
                   timeout, '64-bit counter snapshot did not become valid')
        try:
            frames = (self._control.read(self.REG_TOTAL_FRAMES_HI) << 32) | self._control.read(self.REG_TOTAL_FRAMES_LO)
            words = (self._control.read(self.REG_TOTAL_WORDS_HI) << 32) | self._control.read(self.REG_TOTAL_WORDS_LO)
        finally:
            self._control.write(self.REG_SNAPSHOT, 0)
        if frames < self._last_snapshot['frames'] or words < self._last_snapshot['words']:
            raise RuntimeError('64-bit capture counters moved backwards')
        if words > frames * 4:
            raise RuntimeError('AXI word counter is ahead of admitted four-channel frames')
        self._last_snapshot = {'frames': frames, 'words': words}
        return dict(self._last_snapshot)

    def _next_descriptor_status(self):
        slot = self._next_read_index % self._slot_count
        descriptor = self._descriptors[slot]
        descriptor.invalidate()
        status = int(descriptor[self.BD_STATUS])
        if status & self.BD_INTERNAL_ERROR:
            raise RuntimeError('S2MM descriptor reports an internal/slave/decode error')
        if not status & self.BD_COMPLETE:
            return None
        if status & (self.BD_RXSOF | self.BD_RXEOF) != (self.BD_RXSOF | self.BD_RXEOF):
            raise RuntimeError('S2MM descriptor does not contain one complete packet')
        length = status & self.BD_LENGTH_MASK
        if length != self._frames_per_descriptor * 16:
            raise RuntimeError('S2MM descriptor byte count differs from the PCM packet size')
        return slot, status, length

    def _check_pattern(self, samples, first_frame):
        import numpy as np

        frames = (np.arange(self._frames_per_descriptor, dtype=np.uint64) + first_frame) & 0xFFFFF
        encoded = (frames[:, None] << 4) | np.arange(4, dtype=np.uint64)[None, :]
        expected = ((encoded ^ 0x800000) - 0x800000).astype(np.int32)
        if not np.array_equal(samples, expected):
            raise RuntimeError('Synthetic PCM sequence/channel check failed at a descriptor boundary')

    def _requeue(self, slot):
        descriptor = self._descriptors[slot]
        self._sample_slots[slot].invalidate()
        descriptor[self.BD_STATUS] = 0
        descriptor[8:] = 0
        descriptor.flush()
        self._slot_states[slot] = 'dma_owned'
        address = int(descriptor.physical_address)
        self._dma.write(self.DMA_TAILDESC_MSB, 0)
        self._dma.write(self.DMA_TAILDESC, address)

    def _consume_next(self, *, return_samples, requeue):
        import numpy as np

        completed = self._next_descriptor_status()
        if completed is None:
            return None
        slot, status, length = completed
        if self._slot_states[slot] != 'dma_owned':
            raise RuntimeError('Descriptor ownership is inconsistent')
        buffer = self._sample_slots[slot]
        buffer.invalidate()
        first_frame = self._packets_received * self._frames_per_descriptor
        if np.any(buffer < -8388608) or np.any(buffer > 8388607):
            raise RuntimeError('PCM signed-24 sign extension is invalid')
        if self._source == 'pattern':
            self._check_pattern(buffer, first_frame)
        samples = np.array(buffer, dtype=np.int32, copy=True, subok=False) if return_samples else None
        if samples is not None and (samples.shape != (self._frames_per_descriptor, 4)
                                    or samples.dtype != np.dtype(np.int32)):
            raise RuntimeError('PCM slot does not contain the configured four-channel format')
        self._slot_states[slot] = 'cpu_owned'
        packet_index = self._packets_received
        self._next_read_index += 1
        self._packets_received += 1
        self._bytes_received += length
        if return_samples:
            self._packets_returned += 1
        else:
            self._packets_drained += 1
        counters = self._capture_snapshot()
        if counters['words'] < self._bytes_received // 4:
            raise RuntimeError('DMA descriptor completed ahead of the accepted AXI word counter')
        if requeue:
            self._requeue(slot)
        metadata = {
            'packet_index': packet_index,
            'first_frame': first_frame,
            'frames': self._frames_per_descriptor,
            'bytes': length,
            'sample_rate_hz': self.PCM_RATE_HZ,
            'format': 'pcm_s24_in_s32',
            'descriptor_status': status,
            'counters': counters,
            'source': self._source,
            'build_id': self.clock.identity.build_id,
        }
        return ContinuousPcmPacket(samples, metadata)

    def read_packet(self, timeout=None):
        """Wait for the next ordered descriptor and return a detached PCM array."""
        if self._state != 'running':
            raise ContinuousCaptureError('Start a continuous capture before read_packet()',
                                         self._diagnostics())
        if timeout is None:
            timeout = max(1.0, 4 * self._frames_per_descriptor / self.PCM_RATE_HZ)
        self._positive_timeout(timeout)
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._check_health()
                packet = self._consume_next(return_samples=True, requeue=True)
                if packet is not None:
                    return packet
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('No complete PCM descriptor before the deadline')
                time.sleep(min(0.001, remaining))
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                self._recover_interrupted()
                raise
            self._raise_after_recovery('Continuous PCM packet read failed: ' + str(error), error)

    def _drain_ready(self):
        drained = 0
        while True:
            packet = self._consume_next(return_samples=False, requeue=False)
            if packet is None:
                return drained
            drained += 1

    def _snapshot_health(self):
        self._check_health()
        return self._capture_snapshot()

    def stop(self, timeout=5.0):
        """Stop at an HW packet boundary, drain completed descriptors, validate totals."""
        if self._state == 'stopped' and self._last_result is not None:
            return self._last_result
        if self._state != 'running':
            raise ContinuousCaptureError('No continuous PCM session is running', self._diagnostics())
        self._positive_timeout(timeout)
        try:
            self._control.write(0x24, self.CMD_STOP)
            self._state = 'stopping'
            deadline = time.monotonic() + timeout
            # Continue returning completed slots until hardware reports that
            # the packet-boundary stop and AXI Stream drain have completed.
            while True:
                self._check_health()
                packet = self._consume_next(return_samples=False, requeue=True)
                status = self._control.read(0x28)
                if status & self.STATUS_DONE and not status & self.STATUS_BUSY:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Capture did not stop and drain before the deadline')
                if packet is None:
                    time.sleep(min(0.001, remaining))

            # Halt S2MM only after the producer reports a clean drain so the
            # final descriptor writebacks are stable before inspecting them.
            self._dma.write(self.DMA_CR, 0)
            self._wait(lambda: bool(self._dma.read(self.DMA_SR) & self.DMA_HALTED),
                       min(timeout, self._recovery_timeout), 'AXI DMA did not halt after STOP')
            self._check_health()
            self._drain_ready()
            counters = self._snapshot_health()
            expected_words = counters['words']
            if not self._packets_received:
                raise RuntimeError('STOP completed without any complete PCM descriptor')
            if (counters['frames'] * 16 != self._bytes_received
                    or expected_words * 4 != self._bytes_received
                    or counters['frames'] * 4 != expected_words):
                raise RuntimeError('Final frame, AXI word, descriptor byte, and channel counts disagree')
            if self._packets_received * self._frames_per_descriptor != counters['frames']:
                raise RuntimeError('Descriptor sequence length differs from the 64-bit frame total')
            result = ContinuousCaptureResult({
                'source': self._source,
                'format': 'pcm_s24_in_s32',
                'sample_rate_hz': self.PCM_RATE_HZ,
                'frames_per_descriptor': self._frames_per_descriptor,
                'slot_count': self._slot_count,
                'packets_received': self._packets_received,
                'packets_returned': self._packets_returned,
                'packets_drained_without_return': self._packets_drained,
                'frames_total': counters['frames'],
                'words_total': counters['words'],
                'bytes_received': self._bytes_received,
                'overflows': self._control.read(0x54),
                'clips': self._control.read(0x58),
                'counters': counters,
                'cyclic_dma_mode': False,
                'build_id': self.clock.identity.build_id,
                'host_execution_start_utc': self._started_utc,
                'host_execution_end_utc': datetime.now(timezone.utc).isoformat(),
                'provenance': dict(self._provenance),
            })
            self._last_result = result
            self._state = 'stopped'
            self._release_buffers(safe=True)
            return result
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                self._recover_interrupted()
                raise
            self._raise_after_recovery('Continuous PCM stop/validation failed: ' + str(error), error)

    def _recover_hardware(self):
        if self._control.read(0x28) & self.STATUS_BUSY:
            self._control.write(0x24, self.CMD_ABORT)
        self._dma.write(self.DMA_CR, self.DMA_RESET)
        self._wait(lambda: not self._dma.read(self.DMA_CR) & self.DMA_RESET
                   and bool(self._dma.read(self.DMA_SR) & self.DMA_HALTED)
                   and not self._control.read(0x28) & self.STATUS_BUSY,
                   self._recovery_timeout, 'Continuous PCM abort/DMA reset did not finish')
        if self._dma.read(self.DMA_SR) & self.DMA_ERRORS:
            self._dma.write(self.DMA_SR, self.DMA_W1C)
        self._control.write(0x24, self.CMD_CLEAR)
        self._wait(lambda: not self._control.read(0x28) & self.STATUS_BUSY,
                   self._recovery_timeout, 'Continuous PCM CLEAR did not finish')
        if self._control.read(0x2C) or self._control.read(0x54):
            raise RuntimeError('Continuous PCM errors remained after recovery')

    def _release_buffers(self, *, safe):
        roots = [buffer for buffer in (self._sample_root, self._descriptor_root) if buffer is not None]
        if not safe:
            _UNSAFE_CONTINUOUS_BUFFERS.extend(roots)
            self._retained_buffers.extend(roots)
        self._sample_slots = []
        self._descriptors = []
        self._slot_states = []
        self._sample_root = self._descriptor_root = None
        if safe:
            for buffer in roots:
                buffer.freebuffer()

    def _free_retained_buffers(self):
        for buffer in self._retained_buffers:
            buffer.freebuffer()
        retained_ids = {id(buffer) for buffer in self._retained_buffers}
        _UNSAFE_CONTINUOUS_BUFFERS[:] = [buffer for buffer in _UNSAFE_CONTINUOUS_BUFFERS
                                        if id(buffer) not in retained_ids]
        self._retained_buffers.clear()

    def _recover_interrupted(self):
        try:
            self._recover_hardware()
            self._release_buffers(safe=True)
            self._free_retained_buffers()
            self._state = 'idle'
        except Exception:
            self._faulted = True
            self._state = 'faulted'
            self._release_buffers(safe=False)

    def _raise_after_recovery(self, message, cause):
        diagnostics = self._diagnostics(include_sg=True)
        diagnostics['cause'] = '{}: {}'.format(type(cause).__name__, cause)
        try:
            self._recover_hardware()
            diagnostics['recovery'] = 'ok'
            self._release_buffers(safe=True)
            self._free_retained_buffers()
            self._state = 'idle'
        except Exception as recovery_error:
            diagnostics['recovery'] = str(recovery_error)
            self._faulted = True
            self._state = 'faulted'
            self._release_buffers(safe=False)
        raise ContinuousCaptureError(message, diagnostics) from cause

    def recover(self):
        """Abort and reset the owned S2MM stream while retaining clock/filter state."""
        if self._state in ('starting', 'running', 'stopping', 'faulted'):
            try:
                self._recover_hardware()
            except Exception as error:
                self._faulted = True
                self._state = 'faulted'
                self._release_buffers(safe=False)
                raise ContinuousCaptureError('Continuous PCM recovery failed: ' + str(error),
                                             self._diagnostics()) from error
            self._release_buffers(safe=True)
            self._free_retained_buffers()
        self._faulted = False
        self._state = 'idle'
