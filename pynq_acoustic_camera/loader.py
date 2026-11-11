"""Pinned release acquisition and explicit, generic PYNQ Overlay loading.

No configuration is read from the notebook working directory. Downloading does
not program the FPGA; only load_overlay() imports PYNQ and requests programming.
"""
from contextlib import contextmanager
from importlib import resources
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


class HardwareConfigError(ValueError):
    """Missing, unsupported or incompatible installed hardware selection."""


class HardwareIntegrityError(ValueError):
    """Cached or downloaded hardware does not match the pinned identity."""


class HardwareDownloadError(RuntimeError):
    """The exact pinned release could not be acquired."""


_MAX_MANIFEST = 1024 * 1024
_MAX_BIT = 64 * 1024 * 1024
_MAX_HWH = 16 * 1024 * 1024
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_HEX40 = re.compile(r'[0-9a-f]{40}\Z')
_COMPONENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


def _require(condition, message, error=HardwareConfigError):
    if not condition:
        raise error(message)


def _read_json(data, description, error):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise error('Invalid ' + description) from exc
    _require(isinstance(value, dict), description + ' must be a JSON object', error)
    return value


def _read_file(path, maximum):
    _require(path.is_file() and not path.is_symlink(),
             'Missing or linked artifact: ' + str(path), HardwareIntegrityError)
    _require(0 < path.stat().st_size <= maximum,
             'Invalid artifact size: ' + str(path), HardwareIntegrityError)
    return path.read_bytes()


def _download(url, destination, maximum):
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'pynq-acoustic-camera/0.1'})
        total = 0
        with urllib.request.urlopen(request, timeout=30) as response, destination.open('xb') as out:
            while True:
                block = response.read(65536)
                if not block:
                    break
                total += len(block)
                if total > maximum:
                    raise HardwareIntegrityError('Download exceeds expected size: ' + destination.name)
                out.write(block)
        _require(total > 0, 'Empty download: ' + destination.name, HardwareIntegrityError)
    except (OSError, urllib.error.URLError) as exc:
        raise HardwareDownloadError('Could not download pinned artifact: ' + url) from exc


class HardwareLoader:
    """Use the installed release pin; never select an old or latest release implicitly."""

    @classmethod
    def get_hardware_config(cls):
        try:
            data = resources.files('pynq_acoustic_camera.data').joinpath('hardware.json').read_bytes()
        except (OSError, ModuleNotFoundError) as exc:
            raise HardwareConfigError('Installed hardware.json is missing') from exc
        config = _read_json(data, 'installed hardware configuration', HardwareConfigError)
        required = {'schema_version', 'loadable', 'repository', 'release_tag', 'board', 'part',
                    'overlay_name', 'top', 'manifest_file', 'manifest_sha256',
                    'expected_source_commit', 'expected_interface_id',
                    'expected_build_id_128', 'files'}
        _require(required <= config.keys() and not config.get('template', False),
                 'Hardware configuration is incomplete or a template')
        _require(config['schema_version'] == 1 and config['loadable'] is True,
                 'Hardware configuration is not loadable')
        repo = config['repository']
        _require(isinstance(repo, str) and len(repo.split('/')) == 2 and
                 all(_COMPONENT.fullmatch(x) for x in repo.split('/')), 'Invalid repository')
        for field in ['release_tag', 'board', 'overlay_name']:
            _require(isinstance(config[field], str) and _COMPONENT.fullmatch(config[field]),
                     'Invalid path component: ' + field)
        _require(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[1-9][0-9]*)?',
                             config['release_tag']), 'Expected an exact version tag')
        _require(isinstance(config['manifest_sha256'], str) and
                 _HEX64.fullmatch(config['manifest_sha256']), 'Invalid pinned manifest hash')
        _require(isinstance(config['expected_source_commit'], str) and
                 _HEX40.fullmatch(config['expected_source_commit']), 'Invalid source commit')
        _require(config['board'] == 'pynq_z2' and config['part'] == 'xc7z020clg400-1' and
                 config['top'] == 'xadc_wrapper' and config['expected_interface_id'] ==
                 'legacy-xadc-reference/1' and config['expected_build_id_128'] is None,
                 'This loader currently supports only the phase-1 XADC reference')
        _require(config['manifest_file'] == 'build-manifest.json' and config['files'] == {
            'bit': config['overlay_name'] + '.bit', 'hwh': config['overlay_name'] + '.hwh'},
            'Overlay filenames do not match configured overlay_name')
        return config

    @staticmethod
    def get_board_name():
        """Return a declared board name, or None; never assume a board on a PC."""
        name = os.environ.get('BOARD')
        if not name:
            try:
                name = Path('/etc/board.name').read_text().strip()
            except OSError:
                return None
        return name.strip().lower().replace('-', '_') or None

    @staticmethod
    def _manifest(folder, config):
        data = _read_file(folder / config['manifest_file'], _MAX_MANIFEST)
        _require(hashlib.sha256(data).hexdigest() == config['manifest_sha256'],
                 'Pinned manifest SHA-256 mismatch', HardwareIntegrityError)
        manifest = _read_json(data, 'build manifest', HardwareIntegrityError)
        expected = {'schema_version': 1, 'repository': config['repository'],
                    'source_commit': config['expected_source_commit'], 'source_clean': True,
                    'board': config['board'], 'part': config['part'], 'top': config['top'],
                    'interface_id': config['expected_interface_id'],
                    'build_id_128': config['expected_build_id_128']}
        _require(all(key in manifest and manifest[key] == value for key, value in expected.items()),
                 'Manifest identity or interface mismatch', HardwareIntegrityError)
        records = manifest.get('files')
        _require(isinstance(records, list) and len(records) == 2 and
                 all(isinstance(x, dict) for x in records),
                 'Manifest must contain exactly two artifact records', HardwareIntegrityError)
        names = {config['files']['bit'], config['files']['hwh']}
        _require({x.get('name') for x in records} == names,
                 'Manifest has unexpected artifact names', HardwareIntegrityError)
        for record in records:
            maximum = _MAX_BIT if record['name'] == config['files']['bit'] else _MAX_HWH
            size, digest = record.get('size_bytes'), record.get('sha256')
            _require(type(size) is int and 0 < size <= maximum and
                     isinstance(digest, str) and _HEX64.fullmatch(digest),
                     'Invalid artifact record', HardwareIntegrityError)
        return manifest

    @classmethod
    def _validate_cache(cls, folder, config):
        _require(folder.is_dir() and not folder.is_symlink(),
                 'Invalid cache directory: ' + str(folder), HardwareIntegrityError)
        manifest = cls._manifest(folder, config)
        for record in manifest['files']:
            maximum = _MAX_BIT if record['name'] == config['files']['bit'] else _MAX_HWH
            data = _read_file(folder / record['name'], maximum)
            _require(len(data) == record['size_bytes'] and
                     hashlib.sha256(data).hexdigest() == record['sha256'],
                     'Artifact SHA-256/size mismatch: ' + record['name'], HardwareIntegrityError)
        try:
            root = ET.fromstring((folder / config['files']['hwh']).read_bytes())
        except ET.ParseError as exc:
            raise HardwareIntegrityError('Invalid root HWH XML') from exc
        instances = {m.get('INSTANCE'): m.get('MODTYPE') for m in root.findall('.//MODULE')}
        _require(root.tag == 'EDKSYSTEM' and all(instances.get(name) == 'axi_dma'
                 for name in ['axi_dma_0', 'axi_dma_1', 'axi_dma_2']),
                 'HWH does not describe the legacy XADC reference', HardwareIntegrityError)
        return manifest

    @staticmethod
    def _cache_path(config, download_dir):
        if download_dir is None:
            base = Path(os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache'))
            _require(base.is_absolute(), 'XDG_CACHE_HOME must be absolute')
            base = base / 'pynq_acoustic_camera'
        else:
            base = Path(download_dir).expanduser().absolute() / 'pynq_acoustic_camera'
        owner, repo = config['repository'].split('/')
        return base / owner / repo / config['board'] / config['release_tag'] / config['manifest_sha256']

    @classmethod
    @contextmanager
    def _prepared(cls, version=None, download_dir=None, force_download=False):
        config = cls.get_hardware_config()
        _require(version is None or version == config['release_tag'],
                 'Version overrides are not allowed; install an explicitly repinned package')
        folder = cls._cache_path(config, download_dir)
        folder.parent.mkdir(parents=True, exist_ok=True)
        # Linux/PYNQ process lock: hold it through validation and, for load_overlay,
        # through Overlay construction so another caller cannot replace this pair.
        import fcntl
        with (folder.parent / (folder.name + '.lock')).open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _require(not folder.is_symlink(), 'Cache directory is linked', HardwareIntegrityError)
            if force_download or not folder.exists():
                base = ('https://github.com/' + config['repository'] + '/releases/download/' +
                        config['release_tag'] + '/')
                with tempfile.TemporaryDirectory(prefix='.' + folder.name + '-',
                                                 dir=str(folder.parent)) as temp:
                    staging = Path(temp)
                    _download(base + config['manifest_file'], staging / config['manifest_file'],
                              _MAX_MANIFEST)
                    manifest = cls._manifest(staging, config)  # authenticate before artifact download
                    for record in manifest['files']:
                        _download(base + record['name'], staging / record['name'], record['size_bytes'])
                    cls._validate_cache(staging, config)
                    backup = None
                    if folder.exists():
                        _require(folder.is_dir(), 'Cache path is not a directory', HardwareIntegrityError)
                        backup = Path(tempfile.mkdtemp(prefix='.previous-', dir=str(folder.parent)))
                        backup.rmdir()
                        os.replace(folder, backup)
                    try:
                        os.replace(staging, folder)
                    except OSError:
                        if backup is not None:
                            os.replace(backup, folder)
                        raise
                    else:
                        if backup is not None:
                            shutil.rmtree(backup)
            # Corrupt existing cache fails closed. Explicit force_download replaces
            # only this pinned entry after a complete validated download succeeds.
            manifest = cls._validate_cache(folder, config)
            yield folder, config, manifest

    @classmethod
    def get_overlay_path(cls, version=None, download_dir=None, *, force_download=False):
        """Download/revalidate pinned BIT/HWH and return the BIT path; no programming.

        download_dir is a cache base, not a direct BIT directory. The full pinned
        identity is always appended. Invalid cache raises HardwareIntegrityError;
        use force_download=True for an explicit replacement of this entry only.
        """
        with cls._prepared(version, download_dir, force_download) as (folder, config, _):
            return folder / config['files']['bit']

    @classmethod
    def get_manifest(cls, version=None, download_dir=None):
        """Return the authenticated manifest, acquiring the release if necessary."""
        with cls._prepared(version, download_dir) as (_, _, manifest):
            return manifest

    @classmethod
    def load_overlay(cls, version=None, download_dir=None, *, board=None,
                     force_download=False, **overlay_kwargs):
        """Explicitly program a generic Overlay after identity/integrity checks.

        This does not construct the legacy XADC driver or write its custom control
        registers. PYNQ's normal Overlay/IP initialization still applies.
        """
        config = cls.get_hardware_config()
        _require(board is None or isinstance(board, str), 'board must be a name or None')
        detected = cls.get_board_name()
        selected = board.strip().lower().replace('-', '_') if isinstance(board, str) else detected
        _require(selected == config['board'],
                 'Board not confirmed as pynq_z2; pass board="pynq_z2" explicitly if detection is unavailable')
        _require(detected is None or detected == selected, 'Detected board differs from requested board')
        try:
            from pynq import Overlay
        except ImportError as exc:
            raise RuntimeError('PYNQ is required for explicit FPGA programming') from exc
        with cls._prepared(version, download_dir, force_download) as (folder, pinned, _):
            return Overlay(str(folder / pinned['files']['bit']), **overlay_kwargs)
