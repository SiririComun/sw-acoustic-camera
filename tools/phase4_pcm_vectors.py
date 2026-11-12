#!/usr/bin/env python3
"""Deterministic PCM vectors, independently checked against direct convolution."""
import argparse
import csv
import hashlib
import json
import math
import random
from pathlib import Path
from pcm_fixed_reference import PdmPcmReference, round_shift_ties_to_even


def round_independent(value):
    q, r = divmod(value, 32)
    return q + int(r > 16 or (r == 16 and q % 2 == 1))


def quantize(value):
    rounded = round_independent(value)
    return min(8388607, max(-8388608, rounded)), int(not -8388608 <= rounded <= 8388607)


def sequences():
    yield 'positive_dc_integrator_wrap', [15] * 30000
    yield 'negative_dc', [0] * 4000
    rng = random.Random(4301)
    yield 'identical_random', [15 * rng.getrandbits(1) for _ in range(8000)]
    rng = random.Random(4302)
    distinct = [rng.randrange(16) for _ in range(10000)]
    yield 'distinct_random', distinct
    impulse = [15 * (n % 2) for n in range(4500)]
    for channel, index in enumerate((1750, 1755, 1760, 1775)):
        impulse[index] ^= 1 << channel
    yield 'channel_impulses', impulse
    states = [0.0] * 4
    tones = []
    for n in range(25000):
        mask = 0
        for c, frequency in enumerate((1000, 2000, 3000, 47000)):
            states[c] += 0.7 * math.sin(2 * math.pi * frequency * n / 2400000)
            bit = int(states[c] >= 0)
            states[c] -= 2 * bit - 1
            mask |= bit << c
        tones.append(mask)
    yield 'pdm_tones_and_alias_image', tones
    yield 'identical_tone', [15 * (v & 1) for v in tones[:10000]]
    yield 'reset_then_short_capture_with_ce_gaps', distinct[:1000]


def generate(directory, design):
    directory.mkdir(parents=True, exist_ok=True)
    spec = json.loads(design.read_text())
    coeff = spec['fixed_point']['fir_coefficients']
    cic_kernel = [1, 3, 6, 10, 15, 18, 19, 18, 15, 10, 6, 3, 1]
    cases = []
    with (directory/'inputs.txt').open('w') as inputs, (directory/'expected.csv').open('w', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['case_id', 'index', 'valid', 'ready', 'clips', 'mic0', 'mic1', 'mic2', 'mic3'])
        for case_id, (name, masks) in enumerate(sequences(), 1):
            oracle = PdmPcmReference(design)
            bits_history = [[0]*13 for _ in range(4)]
            fir_history = [[0]*61 for _ in range(4)]
            count = clips_total = 0
            for n, mask in enumerate(masks):
                gap = (10 + (n % 7)) if case_id == 8 else 10
                inputs.write(f'{case_id} {gap} {mask}\n')
                bits = [(mask >> c) & 1 for c in range(4)]
                observed = oracle.feed(bits)
                for c in range(4):
                    bits_history[c] = [2*bits[c]-1] + bits_history[c][:-1]
                if n % 5 == 4:
                    for c in range(4):
                        cic = sum(a*b for a, b in zip(bits_history[c], cic_kernel))
                        fir_history[c] = [cic] + fir_history[c][:-1]
                if n % 50 != 49:
                    assert observed is None
                    continue
                values = [quantize(sum(a*b for a, b in zip(h, coeff))) for h in fir_history]
                samples = tuple(v for v, _ in values)
                valid = int(count >= 6)
                clips = sum(bit << c for c, (_, bit) in enumerate(values)) if valid else 0
                assert observed.index == count and observed.samples == samples
                assert observed.valid == bool(valid)
                assert observed.frame_clip_count == clips.bit_count()
                assert oracle.filter_ready == (count >= 5)
                writer.writerow([case_id, count, valid, int(count >= 5), clips, *samples])
                count += 1
                clips_total += clips.bit_count()
            assert oracle.clip_count == clips_total
            cases.append(dict(id=case_id, name=name, pdm_frames=len(masks), pcm_frames=count,
                              accepted_frames=max(0, count-6), clipped_channel_samples=clips_total))
    with (directory/'rounding.txt').open('w') as out:
        for q in (-8388609, -8388608, -3, -2, -1, 0, 1, 2, 3, 8388607, 8388608):
            for residue in range(32):
                acc = q*32 + residue
                rounded = round_independent(acc)
                sample, clipped = quantize(acc)
                assert round_shift_ties_to_even(acc, 5) == rounded
                out.write(f'{acc} {rounded} {sample} {clipped}\n')
        for acc in (-2147483648, 2147483647):
            sample, clipped = quantize(acc)
            out.write(f'{acc} {round_independent(acc)} {sample} {clipped}\n')
    result = dict(cases=cases, pcm_frames=sum(c['pcm_frames'] for c in cases),
                  accepted_frames=sum(c['accepted_frames'] for c in cases), rounding_cases=354,
                  independent_check='direct CIC FIR13 convolution then FIR61, compared to modular reference',
                  coefficients_sha256_le_i32=spec['fixed_point']['fir_coefficient_sha256_le_i32'])
    (directory/'vector-report.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


def verify(directory, trace):
    with (directory/'expected.csv').open() as f:
        expected = list(csv.DictReader(f))
    with trace.open() as f:
        actual = list(csv.DictReader(f))
    if len(actual) != len(expected):
        raise AssertionError(f'PCM frame count: observed {len(actual)}, expected {len(expected)}')
    for index, (want, got) in enumerate(zip(expected, actual)):
        if {k: int(v) for k, v in want.items()} != {k: int(v) for k, v in got.items()}:
            raise AssertionError(f'Frame {index}: observed {got}, expected {want}')
    return dict(status='pass', frames_checked=len(expected), samples_checked=4*len(expected),
                accepted_clipped_channel_samples=sum(int(r['clips']).bit_count() for r in expected),
                trace_sha256=hashlib.sha256(trace.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['generate', 'verify'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--design', type=Path)
    parser.add_argument('--trace', type=Path)
    args = parser.parse_args()
    if args.command == 'generate':
        result = generate(args.directory, args.design)
    else:
        result = verify(args.directory, args.trace)
        (args.directory/'verification-report.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
