#!/usr/bin/env python3
"""Play a phase-8 contrast WAV sequence from a ready phone browser and log ACKs."""
import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen


def get_json(url):
    with urlopen(url, timeout=3) as response:
        return json.load(response)


def post_json(url, value):
    request = Request(url, json.dumps(value).encode(),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=3) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8765')
    parser.add_argument('--baseline-seconds', type=float, default=8.0)
    parser.add_argument('--gap-seconds', type=float, default=1.0)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if manifest.get('phase_step') not in ('8.7', '8.8', '8.10'):
        raise ValueError('Expected phase 8.7, 8.8, or 8.10 manifest')
    entries = manifest.get('randomized_entries', manifest.get('files', []))
    if not entries:
        raise ValueError('Manifest has no playback entries')
    server = args.server.rstrip('/')
    state = get_json(server + '/state')
    if not state['ready'] or state['phone_seen_monotonic_ns'] is None:
        raise RuntimeError('Phone browser has not enabled audio')
    if time.monotonic_ns() - state['phone_seen_monotonic_ns'] > 3_000_000_000:
        raise RuntimeError('Phone browser is no longer polling')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8', buffering=1) as log:
        def record(event, **details):
            log.write(json.dumps({'event': event, 'pc_monotonic_ns': time.monotonic_ns(),
                                  **details}) + '\n')

        record('baseline_start', seconds=args.baseline_seconds)
        time.sleep(args.baseline_seconds)
        for entry in entries:
            state = get_json(server + '/state')
            if not state['ready'] or time.monotonic_ns() - state['phone_seen_monotonic_ns'] > 3_000_000_000:
                raise RuntimeError('Phone browser stopped polling before ' + entry['file'])
            reply = post_json(server + '/play', {'file': entry['file']})
            command_id = reply['id']
            record('play_sent', command_id=command_id, **entry)
            deadline = time.monotonic() + 12
            started = False
            while time.monotonic() < deadline:
                state = get_json(server + '/state')
                ack = state['last_ack']
                if ack and ack['id'] == command_id:
                    if ack['status'] == 'blocked':
                        raise RuntimeError('Phone audio blocked: ' + str(ack))
                    if ack['status'] == 'started' and not started:
                        started = True
                        record('play_started', command_id=command_id, ack=ack)
                    if ack['status'] == 'ended':
                        record('play_ended', command_id=command_id, ack=ack)
                        break
                time.sleep(0.1)
            else:
                raise TimeoutError('No ended ACK for ' + entry['file'])
            time.sleep(args.gap_seconds)
        record('sweep_done')
    print('CONTRAST_SWEEP_DONE entries={}'.format(len(entries)), flush=True)


if __name__ == '__main__':
    main()
