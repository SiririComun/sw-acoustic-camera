"""Driver for the Phase 11 PL one-shot active-buzzer output."""
import math
import time


class BuzzerPulse:
    """Configure and fire one bounded pulse on PYNQ-Z2 Arduino AR2/U13.

    The PL core runs at 100 MHz. The default output width is 5 ms; the core
    accepts widths from 10 ns through 1 s and does not repeat automatically.
    The external transistor and buzzer circuit must match the hardware guide.
    """

    REG_COMMAND = 0x00
    REG_WIDTH_CYCLES = 0x04
    REG_STATUS = 0x08
    REG_START_TICK_LO = 0x0C
    REG_START_TICK_HI = 0x10
    CLOCK_HZ = 100_000_000
    MIN_WIDTH_CYCLES = 10
    MAX_WIDTH_CYCLES = 100_000_000

    STATUS_ACTIVE = 1 << 0
    STATUS_DONE = 1 << 1
    STATUS_ERROR = 1 << 2

    CMD_START = 1 << 0
    CMD_ABORT = 1 << 1
    CMD_CLEAR = 1 << 2

    def __init__(self, mmio):
        if not callable(getattr(mmio, 'read', None)) or not callable(getattr(mmio, 'write', None)):
            raise TypeError('mmio must provide read(offset) and write(offset, value)')
        self._mmio = mmio

    @classmethod
    def from_overlay(cls, overlay):
        """Attach to an already loaded overlay; this method never programs PL."""
        if not callable(getattr(overlay, 'is_loaded', None)) or not overlay.is_loaded():
            raise RuntimeError('The pulse-enabled overlay must already be loaded')
        try:
            ip = overlay.ip_dict['acoustic_buzzer_pulse']
        except (AttributeError, KeyError) as error:
            raise RuntimeError('Overlay HWH does not expose acoustic_buzzer_pulse') from error
        from pynq import MMIO
        return cls(MMIO(int(ip['phys_addr']), int(ip['addr_range'])))

    @staticmethod
    def _cycles(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('pulse width must be a finite number')
        cycles = int(round(value))
        if not BuzzerPulse.MIN_WIDTH_CYCLES <= cycles <= BuzzerPulse.MAX_WIDTH_CYCLES:
            raise ValueError('pulse width must be in 10..100000000 FCLK3 cycles')
        return cycles

    def configure_cycles(self, cycles):
        """Set the one-shot width in 100 MHz FCLK3 cycles."""
        value = self._cycles(cycles)
        self._mmio.write(self.REG_WIDTH_CYCLES, value)
        return value

    def configure_ms(self, duration_ms):
        """Set pulse width in milliseconds, without changing the 5 V rail."""
        if (isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float))
                or not math.isfinite(duration_ms) or duration_ms <= 0):
            raise ValueError('duration_ms must be finite and positive')
        return self.configure_cycles(duration_ms * self.CLOCK_HZ / 1000.0)

    @property
    def width_cycles(self):
        return self._mmio.read(self.REG_WIDTH_CYCLES)

    @property
    def status_word(self):
        return self._mmio.read(self.REG_STATUS)

    @property
    def status(self):
        value = self.status_word
        return {'active': bool(value & self.STATUS_ACTIVE),
                'done': bool(value & self.STATUS_DONE),
                'error': bool(value & self.STATUS_ERROR)}

    @property
    def start_tick(self):
        """Return the latched FCLK3 cycle count at the electrical start command."""
        low = self._mmio.read(self.REG_START_TICK_LO)
        high = self._mmio.read(self.REG_START_TICK_HI)
        return (high << 32) | low

    def clear_status(self):
        self._mmio.write(self.REG_COMMAND, self.CMD_CLEAR)

    def abort(self):
        self._mmio.write(self.REG_COMMAND, self.CMD_ABORT)

    def fire(self, *, wait=True, timeout_s=2.0):
        """Emit one pulse and return its electrical FCLK3 start timestamp.

        If ``wait`` is true, block until the PL counter completes the configured
        width. No automatic retry or repeated tone is generated.
        """
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError('timeout_s must be finite and positive')
        if self.status['active']:
            raise RuntimeError('A buzzer pulse is already active')
        self.clear_status()
        self._mmio.write(self.REG_COMMAND, self.CMD_START)
        tick = self.start_tick
        if not wait:
            return tick
        deadline = time.monotonic() + timeout_s
        while True:
            state = self.status
            if state['error']:
                raise RuntimeError('The PL rejected or interrupted the buzzer pulse')
            if not state['active'] and state['done']:
                return tick
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Timed out waiting for the PL buzzer pulse to finish')
            time.sleep(min(0.001, remaining))
