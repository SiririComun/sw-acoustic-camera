#!/usr/bin/env python3
"""Interactive LaZ2 trial with timed host stereo playback and evidence copy.

Run in the PC user's terminal. SSH requests sudo interactively on LaZ2; this
tool never reads, stores, or asks for the password itself.
"""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import selectors
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.phase12_simultaneous_tones import main as make_tones

REMOTE = 'xilinx@192.168.2.99'
REMOTE_ROOT = '/home/xilinx/phase12_candidate_20261122'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-sources', type=int, choices=(1, 2), default=2)
    parser.add_argument('--view', choices=('combined', 'source0', 'source1'),
                        default='combined')
    parser.add_argument('--audio-only', action='store_true',
                        help='check PCM and AoA without C920 or HDMI')
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        raise RuntimeError('Run this launcher in an interactive PC terminal')
    if args.max_sources == 1 and args.view == 'source1':
        raise ValueError('source1 requires max_sources=2')

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = WORKSPACE / 'evidence' / 'phase-12-step-04' / ('board-live-' + stamp)
    output.mkdir(parents=True, exist_ok=False)
    stimulus = output / 'stimulus.wav'
    make_tones([str(stimulus), '--seconds', '34'])
    remote_output = REMOTE_ROOT + '/live_' + stamp
    frequencies = '1550 2350' if args.max_sources == 2 else '1550'
    command = (
        'sudo env XILINX_XRT=/usr /usr/local/share/pynq-venv/bin/python3 '
        + REMOTE_ROOT + '/tools/phase12_board_trial.py'
        + ' --output ' + remote_output
        + ' --duration-seconds 45 --max-sources ' + str(args.max_sources)
        + ' --view ' + args.view
        + ' --manual-frequencies ' + frequencies
    )
    if args.audio_only:
        command += ' --audio-only'
    print('LaZ2 trial: {}\nEvidence: {}'.format(remote_output, output), flush=True)
    old_mute = subprocess.run(
        ['pactl', 'get-sink-mute', '@DEFAULT_SINK@'],
        capture_output=True, text=True, check=True).stdout.strip()
    if old_mute not in ('Mute: yes', 'Mute: no'):
        raise RuntimeError('Could not identify the current PC speaker mute state')

    playback = None
    ready = False
    board_exit = None
    try:
        with (output / 'board.log').open('wb') as log:
            board = subprocess.Popen(
                ['ssh', '-tt', '-o', 'ConnectTimeout=10', REMOTE, command],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            selector = selectors.DefaultSelector()
            selector.register(board.stdout, selectors.EVENT_READ)
            tail = b''
            try:
                while selector.get_map():
                    for key, _ in selector.select(timeout=1.0):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            break
                        log.write(chunk)
                        log.flush()
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                        tail = (tail + chunk)[-512:]
                        ready_marker = (b'PHASE12_AUDIO_READY' if args.audio_only
                                        else b'PHASE12_LIVE_READY')
                        if not ready and ready_marker in tail:
                            ready = True
                            subprocess.run(['pactl', 'set-sink-mute', '@DEFAULT_SINK@', '0'],
                                           check=True)
                            playback = subprocess.Popen(
                                ['paplay', '--volume=65536', str(stimulus)])
                            print('\nPC playback started: pulsed stereo 1550 Hz left, '
                                  '2350 Hz right', flush=True)
                    if board.poll() is not None and not selector.get_map():
                        break
            finally:
                selector.close()
                board_exit = board.wait()
    finally:
        if playback is not None:
            if playback.poll() is None:
                playback.terminate()
            playback.wait()
        subprocess.run(['pactl', 'set-sink-mute', '@DEFAULT_SINK@',
                        '1' if old_mute == 'Mute: yes' else '0'], check=True)

    copied = subprocess.run(
        ['scp', '-qr', REMOTE + ':' + remote_output + '/.', str(output)],
        check=False)
    print('board_exit={} evidence_copy_exit={} ready={} path={}'.format(
        board_exit, copied.returncode, ready, output), flush=True)
    if board_exit != 0 or copied.returncode != 0 or not ready:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
