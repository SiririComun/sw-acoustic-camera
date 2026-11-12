#!/usr/bin/env python3
"""Read-only local PCM artifact preflight. Does not import PYNQ or access hardware."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pynq_acoustic_camera.pdm_candidate import _read_json, _artifact_path


def verify(folder):
    folder = Path(folder)
    profile = _read_json(ROOT / 'pynq_acoustic_camera/data/pcm_candidate.json')
    manifest_path = folder / 'candidate-manifest.json'
    manifest = _read_json(manifest_path)
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != profile['manifest_sha256']:
        raise ValueError('PCM manifest differs from the pinned build')
    for key in ('build_id', 'hw_commit', 'part', 'top'):
        if manifest[key] != profile[key]:
            raise ValueError('PCM candidate identity mismatch: ' + key)
    if manifest['artifacts'] != profile['artifacts']:
        raise ValueError('PCM artifact records differ from the pinned pair')
    for record in profile['artifacts']:
        _artifact_path(folder, record, record['path'], 64 * 1024 * 1024)
    notebook = json.loads((ROOT / 'pynq_acoustic_camera/data/notebooks/04_pcm_capture.ipynb').read_text())
    settings = {}
    # The settings cell contains no hardware operations; execute no loading cell.
    exec(compile(''.join(notebook['cells'][1]['source']), 'pcm-notebook-settings', 'exec'), settings)
    for kind in ('BIT', 'HWH'):
        record = next(r for r in profile['artifacts'] if r['path'].endswith('.' + kind.lower()))
        if settings['EXPECTED_' + kind + '_SHA256'] != record['sha256']:
            raise ValueError('Notebook pin differs from candidate: ' + kind)
    if settings['EXPECTED_BUILD_ID'].lower() != profile['build_id']:
        raise ValueError('Notebook BUILD_ID mismatch')
    if any(settings[k] for k in ('RUN_HARDWARE', 'RUN_RAW_CHECK', 'RUN_PATTERN', 'RUN_ACOUSTICS')):
        raise ValueError('Notebook hardware flags must default to false')
    return {'result': 'pass', 'scope': 'pinned_local_files_only_no_hardware',
            'build_id': profile['build_id'], 'hw_commit': profile['hw_commit'],
            'manifest_sha256': profile['manifest_sha256'], 'artifacts': profile['artifacts']}


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: verify_pcm_candidate.py CANDIDATE_DIRECTORY')
    print(json.dumps(verify(sys.argv[1]), indent=2))
