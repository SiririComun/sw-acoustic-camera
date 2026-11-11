"""Validation and explicit loading for the local phase-2 PDM candidate.

This is deliberately separate from :mod:`pynq_acoustic_camera.loader`, which
continues to serve the accepted phase-1 XADC release. Preparing a candidate is
read-only. FPGA programming is available only through ``program_overlay()``.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


class PdmCandidateIntegrityError(ValueError):
    """The candidate manifest, BIT/HWH pair or PDM identity is incompatible."""


_EXPECTED_BUILD_ID = 'b3f22d7ae0844695ad24ee1e24ff9ca0'
_EXPECTED_BIT_SHA256 = '6d0197d636f53169931fa26bb3b2ef6c0e59457166a998def09d3247e6edc5d0'
_EXPECTED_HWH_SHA256 = 'e04547331973c393c34121e41bf08df394178668c03a76e22080b12329f94901'
_EXPECTED_SOURCE_COMMIT = 'f5a88ec95621f7a1967690b92c7481b8fdc6fb8c'
_EXPECTED_IP_INSTANCE = 'acoustic_capture_0'
_EXPECTED_IP_BASE = 0x43C00000
_EXPECTED_PART = 'xc7z020clg400-1'
_EXPECTED_TOP = 'pdm_clock_control_wrapper'
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_BIT_BYTES = 64 * 1024 * 1024
_MAX_HWH_BYTES = 16 * 1024 * 1024
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def _fail(message):
    raise PdmCandidateIntegrityError(message)


def _read_json(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    if path.is_symlink() or not path.is_file():
        _fail('Missing or linked candidate manifest: ' + str(path))
    if not 0 < path.stat().st_size <= _MAX_MANIFEST_BYTES:
        _fail('Invalid candidate manifest size')
    try:
        value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_keys)
    except (OSError, UnicodeError, ValueError) as exc:
        raise PdmCandidateIntegrityError('Invalid candidate manifest JSON') from exc
    if not isinstance(value, dict):
        _fail('Candidate manifest must be a JSON object')
    return value


def _artifact_path(folder, record, expected_name, maximum):
    if not isinstance(record, dict) or record.get('path') != expected_name:
        _fail('Unexpected candidate artifact path: ' + expected_name)
    size = record.get('size_bytes')
    digest = record.get('sha256')
    if type(size) is not int or not 0 < size <= maximum:
        _fail('Invalid size in candidate manifest: ' + expected_name)
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        _fail('Invalid SHA-256 in candidate manifest: ' + expected_name)
    path = folder / expected_name
    if path.is_symlink() or not path.is_file():
        _fail('Missing or linked candidate artifact: ' + str(path))
    if path.stat().st_size != size:
        _fail('Candidate artifact size mismatch: ' + expected_name)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
        _fail('Candidate artifact hash differs from its manifest: ' + expected_name)
    return path, actual


@dataclass(frozen=True)
class PdmCandidate:
    """A locally verified phase-2 candidate and its hardware identity."""

    manifest_path: Path
    bit_path: Path
    hwh_path: Path
    build_id: str
    ip_instance: str
    ip_base_address: int
    bit_sha256: str
    hwh_sha256: str

    def _validate_pair(self):
        """Recheck pinned hashes immediately before asking PYNQ to use the pair."""
        if self.bit_path.stem != self.hwh_path.stem:
            _fail('PDM BIT and HWH must have the same basename')
        records = {
            self.bit_path.name: {'path': self.bit_path.name,
                                 'size_bytes': self.bit_path.stat().st_size,
                                 'sha256': self.bit_sha256},
            self.hwh_path.name: {'path': self.hwh_path.name,
                                 'size_bytes': self.hwh_path.stat().st_size,
                                 'sha256': self.hwh_sha256},
        }
        _artifact_path(self.bit_path.parent, records[self.bit_path.name],
                       self.bit_path.name, _MAX_BIT_BYTES)
        _artifact_path(self.hwh_path.parent, records[self.hwh_path.name],
                       self.hwh_path.name, _MAX_HWH_BYTES)

    def create_overlay(self):
        """Construct an unloaded PYNQ Overlay; this does not program the FPGA."""
        self._validate_pair()
        try:
            from pynq import Overlay
        except ImportError as exc:
            raise RuntimeError('PYNQ is required to create an Overlay object') from exc
        return Overlay(str(self.bit_path), download=False)

    def program_overlay(self, *, confirmed_board):
        """Program this exact candidate after an explicit PYNQ-Z2 confirmation."""
        if confirmed_board != 'pynq_z2':
            raise ValueError('Pass confirmed_board="pynq_z2" to authorize candidate programming')
        from .loader import HardwareLoader
        detected = HardwareLoader.get_board_name()
        if detected is not None and detected != 'pynq_z2':
            raise RuntimeError('Detected board is not pynq_z2: ' + detected)
        self._validate_pair()
        try:
            from pynq import Overlay
        except ImportError as exc:
            raise RuntimeError('PYNQ is required for explicit FPGA programming') from exc
        return Overlay(str(self.bit_path), download=True)


class PdmCandidateLoader:
    """Validate only the exact phase-2 step-2.8 BIT/HWH candidate."""

    @classmethod
    def prepare(cls, manifest_path):
        """Verify the candidate files and HWH mapping without loading hardware."""
        path = Path(manifest_path).expanduser().absolute()
        manifest = _read_json(path)
        build = manifest.get('build')
        artifacts = manifest.get('artifacts')
        if not isinstance(build, dict) or not isinstance(artifacts, list):
            _fail('Candidate manifest is missing build or artifact records')
        if (manifest.get('schema_version') != 1 or manifest.get('step') != '2.8'
                or manifest.get('artifact_kind') !=
                'local PDM phase-2 candidate; not a release and not board-validated'):
            _fail('Manifest is not the phase-2 step-2.8 PDM candidate')
        if (build.get('bitstream_generated') is not True or build.get('board_programmed') is not False
                or build.get('microphones_connected') is not False):
            _fail('Candidate manifest records an unexpected physical/build state')
        if (build.get('source_commit') != _EXPECTED_SOURCE_COMMIT
                or build.get('part') != _EXPECTED_PART or build.get('top') != _EXPECTED_TOP
                or str(build.get('build_id_128', '')).lower() != _EXPECTED_BUILD_ID
                or build.get('control_ip_instance') != _EXPECTED_IP_INSTANCE):
            _fail('Candidate build identity does not match the reviewed step-2.8 candidate')
        try:
            base_address = int(build.get('control_ip_base_address'), 0)
        except (TypeError, ValueError) as exc:
            raise PdmCandidateIntegrityError('Invalid PDM control IP base address') from exc
        if base_address != _EXPECTED_IP_BASE:
            _fail('Candidate PDM control IP base address does not match the reviewed HWH')

        expected = {'pdm_clock_control.bit': _EXPECTED_BIT_SHA256,
                    'pdm_clock_control.hwh': _EXPECTED_HWH_SHA256}
        matches = {name: [item for item in artifacts
                          if isinstance(item, dict) and item.get('path') == name]
                   for name in expected}
        if any(len(items) != 1 for items in matches.values()):
            _fail('Candidate manifest must contain one BIT and one matching HWH record')
        bit_path, bit_hash = _artifact_path(
            path.parent, matches['pdm_clock_control.bit'][0], 'pdm_clock_control.bit',
            _MAX_BIT_BYTES)
        hwh_path, hwh_hash = _artifact_path(
            path.parent, matches['pdm_clock_control.hwh'][0], 'pdm_clock_control.hwh',
            _MAX_HWH_BYTES)
        if bit_hash != _EXPECTED_BIT_SHA256 or hwh_hash != _EXPECTED_HWH_SHA256:
            _fail('Candidate BIT/HWH hashes differ from the reviewed step-2.8 pair')

        try:
            root = ET.fromstring(hwh_path.read_bytes())
        except (OSError, ET.ParseError) as exc:
            raise PdmCandidateIntegrityError('Invalid candidate HWH XML') from exc
        if root.tag != 'EDKSYSTEM':
            _fail('Candidate HWH does not have an EDKSYSTEM root')
        modules = [item for item in root.findall('.//MODULE')
                   if item.get('INSTANCE') == _EXPECTED_IP_INSTANCE]
        if len(modules) != 1 or modules[0].get('MODTYPE') != 'acoustic_pdm_clock_control':
            _fail('Candidate HWH does not describe the expected PDM control IP')
        parameters = {item.get('NAME'): item.get('VALUE')
                      for item in modules[0].findall('./PARAMETERS/PARAMETER')}
        ranges = [item for item in root.findall('.//MEMRANGE')
                  if item.get('INSTANCE') == _EXPECTED_IP_INSTANCE]
        if (parameters.get('C_BASEADDR', '').lower() != '0x43c00000'
                or len(ranges) != 1 or ranges[0].get('BASEVALUE', '').lower() != '0x43c00000'):
            _fail('Candidate HWH PDM control address does not match the reviewed address')

        return PdmCandidate(
            manifest_path=path,
            bit_path=bit_path,
            hwh_path=hwh_path,
            build_id=_EXPECTED_BUILD_ID,
            ip_instance=_EXPECTED_IP_INSTANCE,
            ip_base_address=_EXPECTED_IP_BASE,
            bit_sha256=bit_hash,
            hwh_sha256=hwh_hash,
        )
