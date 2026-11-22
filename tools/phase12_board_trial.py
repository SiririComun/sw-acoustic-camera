#!/usr/bin/env python3
"""Run the Phase 12 technical trial and always restore the verified Base overlay.

Invoke through interactive sudo on LaZ2. The trial is technical integration;
its microphone directions and camera projection are not physically calibrated.
"""

import argparse
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bit', type=Path, default=ROOT / 'base.bit')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=float, default=30.0)
    parser.add_argument('--max-sources', type=int, choices=(1, 2), default=2)
    parser.add_argument('--manual-frequencies', type=float, nargs='*', default=())
    parser.add_argument('--view', choices=('combined', 'source0', 'source1'),
                        default='combined')
    parser.add_argument('--pcm-channels', type=int, nargs=4, default=(0, 1, 2, 3))
    args = parser.parse_args(argv)
    if os.geteuid() != 0 or sys.prefix != '/usr/local/share/pynq-venv':
        raise RuntimeError('Run as root with the PYNQ virtualenv Python')

    import phase6_video_perf as video
    from pynq import Overlay
    from pynq.pl_server.device import Device
    from tools.phase12_live_camera import main as live_main

    video.verify_base_artifacts()
    video.use_legacy_metadata_parser()
    print('PHASE12_BASE_PRELOAD_BEGIN', flush=True)
    official = Overlay(str(video.BASE_BIT), download=False,
                       device=Device.active_device)
    official.download()  # Required even when Base appears to be loaded.
    if not official.is_loaded():
        raise RuntimeError('Official Base preload did not complete')
    print('PHASE12_BASE_PRELOAD_OK bit_sha256={}'.format(
        video.EXPECTED_BASE_BIT_SHA256), flush=True)

    trial_args = [
        '--bit', str(args.bit), '--output', str(args.output),
        '--duration-seconds', str(args.duration_seconds),
        '--max-sources', str(args.max_sources), '--view', args.view,
        '--pcm-channels', *(str(channel) for channel in args.pcm_channels),
        '--manual-frequencies', *(str(frequency) for frequency in args.manual_frequencies),
    ]
    try:
        live_main(trial_args)
    finally:
        print('PHASE12_BASE_RESTORE_BEGIN', flush=True)
        try:
            restored = Overlay(str(video.BASE_BIT), download=False,
                               device=Device.active_device)
            restored.download()
            if not restored.is_loaded():
                raise RuntimeError('Official Base restore did not complete')
            print('PHASE12_BASE_RESTORE_OK bit_sha256={}'.format(
                video.EXPECTED_BASE_BIT_SHA256), flush=True)
        except BaseException:
            print('PHASE12_BASE_RESTORE_FAILED\n' + traceback.format_exc(),
                  file=sys.stderr, flush=True)
            raise


if __name__ == '__main__':
    main()
