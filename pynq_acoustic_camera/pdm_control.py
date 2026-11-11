"""Identity and clock control for the separate PDM hardware candidate.

This module does not load or program an overlay. Pass the MMIO object belonging
to the already loaded PDM control IP explicitly.
"""

from dataclasses import dataclass
import re
import time


class PdmInterfaceError(RuntimeError):
    """The PDM control IP did not behave as described by its interface."""


class PdmIdentityError(PdmInterfaceError):
    """The mapped IP has an incompatible identity or build ID."""


@dataclass(frozen=True)
class PdmIdentity:
    """Identity values read from the PDM control block."""

    magic: int
    abi_version: int
    capabilities: int
    channels: int
    build_id: str

    @property
    def abi_major(self):
        return self.abi_version >> 16

    @property
    def abi_minor(self):
        return self.abi_version & 0xFFFF


class PdmClockControl:
    """Validate a PDM control IP, then expose its identity and clock control.

    ``mmio`` is an existing PYNQ MMIO object (normally ``overlay.<ip>.mmio``).
    Construction performs read-only identity checks and never writes hardware.
    ``expected_build_id`` is required and contains 32 hexadecimal characters,
    most significant word first, as reported by the interface contract.
    """

    REG_MAGIC = 0x00
    REG_ABI_VERSION = 0x04
    REG_CAPABILITIES = 0x08
    REG_CHANNELS = 0x0C
    REG_BUILD_ID_0 = 0x10
    REG_CONTROL = 0x20
    REG_STATUS = 0x28
    REG_PDM_CLOCK_HZ = 0x40
    REG_WARMUP_CYCLES = 0x5C

    EXPECTED_MAGIC = 0x4143414D
    EXPECTED_ABI_MAJOR = 1
    EXPECTED_CHANNELS = 4
    CAP_CLOCK_CONTROL = 1 << 0
    CLOCK_ENABLE = 1 << 0
    CLOCK_READY = 1 << 0

    def __init__(self, mmio, expected_build_id):
        if not callable(getattr(mmio, 'read', None)) or not callable(getattr(mmio, 'write', None)):
            raise TypeError('mmio must provide read(offset) and write(offset, value)')
        self._mmio = mmio
        self.identity = self._read_and_validate_identity(expected_build_id)

    def _read_and_validate_identity(self, expected_build_id):
        words = tuple(self._mmio.read(self.REG_BUILD_ID_0 + 4 * index) & 0xFFFFFFFF
                      for index in range(4))
        identity = PdmIdentity(
            magic=self._mmio.read(self.REG_MAGIC) & 0xFFFFFFFF,
            abi_version=self._mmio.read(self.REG_ABI_VERSION) & 0xFFFFFFFF,
            capabilities=self._mmio.read(self.REG_CAPABILITIES) & 0xFFFFFFFF,
            channels=self._mmio.read(self.REG_CHANNELS) & 0xFFFFFFFF,
            build_id=''.join('{:08X}'.format(word) for word in reversed(words)),
        )

        if identity.magic != self.EXPECTED_MAGIC:
            raise PdmIdentityError('PDM control IP MAGIC does not match')
        if identity.abi_major != self.EXPECTED_ABI_MAJOR:
            raise PdmIdentityError(
                'Unsupported PDM control ABI major version: {}'.format(identity.abi_major)
            )
        if identity.channels != self.EXPECTED_CHANNELS:
            raise PdmIdentityError(
                'Expected 4 PDM channels, IP reports {}'.format(identity.channels)
            )
        if not identity.capabilities & self.CAP_CLOCK_CONTROL:
            raise PdmIdentityError('PDM control IP does not advertise CLOCK_ENABLE')

        if (not isinstance(expected_build_id, str)
                or re.fullmatch(r'[0-9a-fA-F]{32}', expected_build_id) is None):
            raise ValueError('expected_build_id must contain exactly 32 hexadecimal characters')
        if identity.build_id != expected_build_id.upper():
            raise PdmIdentityError('PDM control IP BUILD_ID does not match the expected build')

        return identity

    @property
    def clock_enabled(self):
        """Return the CLOCK_ENABLE control bit."""
        return bool(self._mmio.read(self.REG_CONTROL) & self.CLOCK_ENABLE)

    @property
    def clock_ready(self):
        """Return CLOCK_READY, including MMCM lock and the hardware warmup."""
        return bool(self._mmio.read(self.REG_STATUS) & self.CLOCK_READY)

    @property
    def pdm_clock_hz(self):
        """Return the nominal PDM clock frequency reported by hardware."""
        return self._mmio.read(self.REG_PDM_CLOCK_HZ) & 0xFFFFFFFF

    @property
    def warmup_cycles(self):
        """Return the minimum warmup cycles reported by hardware."""
        return self._mmio.read(self.REG_WARMUP_CYCLES) & 0xFFFFFFFF

    def set_clock_enabled(self, enabled):
        """Set CLOCK_ENABLE and check that the control bit reads back."""
        if type(enabled) is not bool:
            raise TypeError('enabled must be bool')
        value = self.CLOCK_ENABLE if enabled else 0
        self._mmio.write(self.REG_CONTROL, value)
        if self.clock_enabled is not enabled:
            raise PdmInterfaceError('CLOCK_ENABLE did not read back as requested')

    def enable_clock(self):
        """Enable the shared microphone clock; readiness may take warmup time."""
        self.set_clock_enabled(True)

    def disable_clock(self):
        """Disable the shared microphone clock."""
        self.set_clock_enabled(False)

    def wait_clock_ready(self, timeout=1.0, poll_interval=0.005):
        """Wait for CLOCK_READY or raise ``TimeoutError`` at the deadline."""
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValueError('timeout must be a non-negative number of seconds')
        if (isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float))
                or poll_interval <= 0):
            raise ValueError('poll_interval must be a positive number of seconds')

        deadline = time.monotonic() + timeout
        while True:
            if self.clock_ready:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('PDM clock did not become ready within {:.3f} s'.format(timeout))
            time.sleep(min(poll_interval, remaining))
