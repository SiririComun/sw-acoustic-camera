#!/usr/bin/env python3
"""Analyze saved phase 8 board evidence without touching the live system."""
import argparse
import json
import math
from pathlib import Path

import numpy as np


RATE = 48000
FRAMES_PER_PACKET = 4096
HOP = 1024
TRACE_CHUNK = 256


def percentile(values, p):
    data = sorted(float(x) for x in values)
    if not data:
        return None
    pos = (len(data) - 1) * p / 100.0
    low, high = int(math.floor(pos)), int(math.ceil(pos))
    if low == high:
        return data[low]
    return data[low] * (high - pos) + data[high] * (pos - low)


def summarize(values):
    return {'count': len(values), 'p50': percentile(values, 50),
            'p95': percentile(values, 95), 'max': max(values) if values else None}


def read_packets(path):
    return [json.loads(line) for line in Path(path).open(encoding='utf-8') if line.strip()]


def event_intervals(packets, capture_end_frame):
    starts, completed = [], []
    active = None
    windows = []
    for packet in packets:
        for result in packet.get('windows', []):
            end = int(result['end_frame_exclusive'])
            windows.append((max(0, end - HOP), end,
                            bool(result.get('system', {}).get('present', False))))
            event = result.get('system', {}).get('event')
            if not event:
                continue
            if event['type'] == 'start':
                active = {'start_frame': int(event['reported_frame']),
                          'frequency_hz': float(event['frequency_hz']),
                          'start_event': event}
                starts.append(active)
            elif event['type'] == 'end' and active is not None:
                completed.append({**active, 'end_frame': int(event['reported_frame']),
                                  'end_event': event, 'open_at_capture_end': False})
                active = None
    if active is not None:
        completed.append({**active, 'end_frame': capture_end_frame,
                          'end_event': None, 'open_at_capture_end': True})
    return completed, windows


def summarize_realtime(status, packets, intervals):
    measurement = status.get('measurement', {})
    queue = [float(p.get('queue_age_estimate_ms_at_return', 0.0)) for p in packets]
    arrivals = [float(p['return_interval_ms']) for p in packets
                if p.get('return_interval_ms') is not None]
    n = min(100, len(queue))
    start_queue = percentile(queue[:n], 50) if n else None
    end_queue = percentile(queue[-n:], 50) if n else None
    capture = measurement.get('final_capture', {})
    duration = float(measurement.get('elapsed_seconds_before_stop', 0.0))
    sustained_growth = bool(start_queue is not None and end_queue is not None
                            and end_queue - start_queue > 10.0)
    loss = (int(capture.get('overflows', 0)) != 0 or int(capture.get('clips', 0)) != 0
            or int(capture.get('packets_received', 0)) !=
            int(capture.get('packets_returned', 0)) +
            int(capture.get('packets_drained_without_return', 0)))
    product_cpu = measurement.get('capture_plus_detector_cpu_ms', {}).get('p95')
    period = float(measurement.get('expected_packet_period_ms', 1000 * FRAMES_PER_PACKET / RATE))
    total_summary = measurement.get('packet_total_cpu_ms', summarize([]))
    return {
        'duration_seconds': duration,
        'packets_returned': len(packets),
        'cadence_ms': summarize(arrivals),
        'product_cpu_ms_per_packet': measurement.get('capture_plus_detector_cpu_ms'),
        'total_process_cpu_ms_per_packet_including_capture_copy_and_json': total_summary,
        'queue_age_ms': summarize(queue),
        'queue_first_100_p50_ms': start_queue,
        'queue_last_100_p50_ms': end_queue,
        'sustained_queue_growth_over_10ms': sustained_growth,
        'packet_period_ms': period,
        'zero_loss_overflow_clip': not loss,
        'base_restored': status.get('base_restored_ok') is True,
        'within_product_cpu_budget': product_cpu is not None and product_cpu <= 70.0,
        'within_total_packet_cadence': total_summary['p95'] is not None and total_summary['p95'] < period,
        'measurement_elapsed_matches_capture': duration > 0,
    }


def negative_result(intervals, duration_seconds):
    false_events = [e for e in intervals
                    if (e['end_frame'] - e['start_frame']) >= round(0.2 * RATE)]
    flickers = [e for e in intervals if e not in false_events]
    rate = len(false_events) / (duration_seconds / 60.0) if duration_seconds else None
    return {
        'system_event_count': len(intervals),
        'false_sustained_events_ge_200ms': len(false_events),
        'false_sustained_event_rate_per_min': rate,
        'short_flicker_count': len(flickers),
        'short_flicker_durations_ms': [1000 * (e['end_frame'] - e['start_frame']) / RATE
                                       for e in flickers],
        'open_event_at_capture_end': any(e['open_at_capture_end'] for e in intervals),
        'within_false_event_limit': rate is not None and rate <= 1.0,
    }


def fixed_frequency_trace(samples, tone_hz):
    frames = (len(samples) // TRACE_CHUNK) * TRACE_CHUNK
    chunks = samples[:frames].reshape(-1, TRACE_CHUNK, 4).astype(np.float64) / (1 << 23)
    chunks -= chunks.mean(axis=1, keepdims=True)
    window = np.hanning(TRACE_CHUNK)
    ref = np.exp(-2j * np.pi * tone_hz * np.arange(TRACE_CHUNK) / RATE) * window
    coeff = 2 * np.einsum('nsc,s->nc', chunks, ref) / window.sum()
    level = 20 * np.log10(np.maximum(np.abs(coeff) / math.sqrt(2.0), 1e-15))
    return coeff, level


def mask_intervals(mask, minimum_chunks, merge_gap_chunks):
    indices = np.flatnonzero(mask)
    if not len(indices):
        return []
    groups = []
    start = previous = int(indices[0])
    for value in indices[1:]:
        value = int(value)
        if value - previous - 1 > merge_gap_chunks:
            if previous - start + 1 >= minimum_chunks:
                groups.append((start, previous + 1))
            start = value
        previous = value
    if previous - start + 1 >= minimum_chunks:
        groups.append((start, previous + 1))
    return groups


def pcm_contrast(samples, tone_hz, start_frame, end_frame):
    per_channel = [[] for _ in range(4)]
    first = max(0, start_frame // FRAMES_PER_PACKET)
    last = min(len(samples) // FRAMES_PER_PACKET,
               math.ceil(end_frame / FRAMES_PER_PACKET))
    frequency = np.fft.rfftfreq(FRAMES_PER_PACKET, 1.0 / RATE)
    window = np.hanning(FRAMES_PER_PACKET)
    band = (frequency >= 1000) & (frequency <= 3000)
    tone = band & (np.abs(frequency - tone_hz) <= 50)
    rest = band & ~tone
    for packet_index in range(first, last):
        packet_start = packet_index * FRAMES_PER_PACKET
        if packet_start + FRAMES_PER_PACKET <= start_frame or packet_start >= end_frame:
            continue
        block = samples[packet_start:packet_start + FRAMES_PER_PACKET].astype(np.float64) / (1 << 23)
        block -= block.mean(axis=0, keepdims=True)
        spectrum = np.fft.rfft(block * window[:, None], axis=0)
        power = np.abs(spectrum) ** 2
        tone_power = np.sum(power[tone], axis=0)
        rest_power = np.sum(power[rest], axis=0)
        ratio = 10 * np.log10(np.maximum(tone_power, 1e-30) / np.maximum(rest_power, 1e-30))
        for channel in range(4):
            per_channel[channel].append(float(ratio[channel]))
    channel_median = [percentile(values, 50) for values in per_channel if values]
    return {
        'per_channel_median_db': channel_median,
        'median_across_channels_db': percentile(channel_median, 50),
        'minimum_channel_median_db': min(channel_median) if channel_median else None,
        'measurement': 'PCM energy within +/-50 Hz of target versus remainder of 1-3 kHz; approximate, not calibrated acoustic SNR',
    }


def phase_frequency(coeff, tone_hz, start_frame, end_frame, min_level_dbfs=-100.0):
    first = max(0, start_frame // TRACE_CHUNK)
    last = min(len(coeff), math.ceil(end_frame / TRACE_CHUNK))
    values = coeff[first:last]
    if len(values) < 3:
        return None
    crosses = values[1:] * np.conj(values[:-1])
    magnitudes = np.abs(values[1:]) * np.abs(values[:-1])
    amplitude_threshold = 10 ** (min_level_dbfs / 20.0)
    good = ((np.abs(values[1:]) > amplitude_threshold)
            & (np.abs(values[:-1]) > amplitude_threshold))
    if not np.any(good):
        return None
    # A fixed-frequency complex demodulator's phase progression is independent
    # of the detector's peak-frequency estimator.
    phase_step = np.angle(np.sum(crosses[good]))
    aliased_hz = phase_step * RATE / (2 * np.pi * TRACE_CHUNK)
    alias_period_hz = RATE / TRACE_CHUNK
    alias_index = round((tone_hz - aliased_hz) / alias_period_hz)
    return float(aliased_hz + alias_index * alias_period_hz)


def tone_result(run_dir, status, packets, intervals, windows, args):
    raw_info = status.get('raw_pcm')
    if not raw_info:
        raise ValueError('Tone analysis requires a run made with --save-raw-pcm')
    raw_path = Path(run_dir) / raw_info['file']
    pcm = np.fromfile(raw_path, dtype='<i4')
    if pcm.size % 4:
        raise ValueError('Saved PCM byte count is not divisible by four channels')
    samples = pcm.reshape(-1, 4)
    coeff, level_dbfs = fixed_frequency_trace(samples, args.tone_hz)
    three_of_four_dbfs = np.sort(level_dbfs, axis=1)[:, 1]
    floor_dbfs = percentile(three_of_four_dbfs.tolist(), 20)
    high_dbfs = percentile(three_of_four_dbfs.tolist(), 80)
    separation = high_dbfs - floor_dbfs
    threshold = floor_dbfs + max(3.0, separation / 2.0)
    above = three_of_four_dbfs >= threshold
    merge_gap = round(0.10 * RATE / TRACE_CHUNK)
    minimum = round(0.80 * RATE / TRACE_CHUNK)
    segments = mask_intervals(above, minimum, merge_gap)
    trace_intervals = [(a * TRACE_CHUNK, b * TRACE_CHUNK) for a, b in segments]
    present_windows = [(a, b) for a, b, is_present in windows if is_present]
    system_starts = [e for e in intervals]
    appearances = []
    event_index = 0
    matched_event_starts = set()
    for appearance_index, (onset, offset) in enumerate(trace_intervals):
        next_onset = (trace_intervals[appearance_index + 1][0]
                      if appearance_index + 1 < len(trace_intervals) else len(samples))
        while event_index < len(system_starts) and system_starts[event_index]['start_frame'] < onset:
            event_index += 1
        matched = None
        if event_index < len(system_starts):
            candidate = system_starts[event_index]
            # Associate by time order across the whole appearance interval.
            # A late event must remain measurable as late; filtering it out at
            # 200 ms hides its frequency and release evidence.
            if candidate['start_frame'] < next_onset:
                matched = candidate
                event_index += 1
                matched_event_starts.add(matched['start_frame'])
        stable_start, stable_end = onset + round(0.02 * RATE), offset - round(0.02 * RATE)
        stable_len = max(0, stable_end - stable_start)
        covered = sum(max(0, min(b, stable_end) - max(a, stable_start))
                      for a, b in present_windows)
        coverage = covered / stable_len if stable_len else 0.0
        contrast = pcm_contrast(samples, args.tone_hz, stable_start, stable_end)
        reference_frequency = phase_frequency(coeff, args.tone_hz, stable_start, stable_end)
        frequency = matched['frequency_hz'] if matched else None
        latency_ms = (1000 * (matched['start_frame'] - onset) / RATE) if matched else None
        matched_end = matched.get('end_frame') if matched else None
        release_measurable = bool(matched and not matched.get('open_at_capture_end', False)
                                  and matched_end is not None)
        release_ms = (1000 * (matched_end - offset) / RATE) if release_measurable else None
        appearances.append({
            'pcm_onset_frame': onset,
            'pcm_offset_frame': offset,
            'stable_duration_ms': 1000 * stable_len / RATE,
            'system_start_frame': matched['start_frame'] if matched else None,
            'detection_latency_ms': latency_ms,
            'system_present_fraction_stable': coverage,
            'detector_frequency_hz': frequency,
            'pcm_reference_frequency_hz': reference_frequency,
            'frequency_error_vs_wav_hz': abs(frequency - args.tone_hz) if frequency is not None else None,
            'frequency_error_vs_pcm_hz': abs(frequency - reference_frequency)
                if frequency is not None and reference_frequency is not None else None,
            'release_latency_after_pcm_offset_ms': release_ms,
            'release_measurable': release_measurable,
            'contrast': contrast,
            'detected_within_200ms': latency_ms is not None and 0 <= latency_ms <= 200,
            'present_for_at_least_90_percent': coverage >= 0.90,
            'frequency_within_20hz_of_wav_and_pcm': (
                frequency is not None and reference_frequency is not None
                and abs(frequency - args.tone_hz) <= 20
                and abs(frequency - reference_frequency) <= 20),
            'released_within_200ms': (release_ms is not None
                                      and 0 <= release_ms <= 200),
            'meets_contrast_floor': (contrast['median_across_channels_db'] is not None
                                     and contrast['median_across_channels_db'] >= args.contrast_floor_db),
        })
    expected = args.expected_appearances
    valid_trace = len(appearances) == expected and separation >= 6.0
    tested = [item for item in appearances if item['meets_contrast_floor']]
    detection_rate = (sum(x['detected_within_200ms'] for x in tested) / len(tested)
                      if tested else 0.0)
    freq_rate = (sum(x['frequency_within_20hz_of_wav_and_pcm'] for x in tested) / len(tested)
                 if tested else 0.0)
    tone_false = [e for e in intervals if e['start_frame'] not in matched_event_starts]
    return {
        'tone_hz': args.tone_hz,
        'contrast_floor_db': args.contrast_floor_db,
        'pcm_trace_threshold_dbfs': threshold,
        'pcm_trace_baseline_p20_dbfs': floor_dbfs,
        'pcm_trace_active_p80_dbfs': high_dbfs,
        'pcm_trace_separation_db': separation,
        'expected_appearances': expected,
        'pcm_confirmed_appearances': len(appearances),
        'pcm_trace_valid': valid_trace,
        'appearances_meeting_contrast_floor': len(tested),
        'detection_rate_within_200ms': detection_rate,
        'frequency_within_20hz_rate': freq_rate,
        'nonmatching_system_events_in_tone_run': len(tone_false),
        'appearances': appearances,
        'criteria': {
            'at_least_90_percent_detected_within_200ms': valid_trace and len(tested) == expected and detection_rate >= 0.90,
            'at_least_90_percent_frequency_within_20hz': valid_trace and len(tested) == expected and freq_rate >= 0.90,
            'all_present_fraction_at_least_90_percent': valid_trace and len(tested) == expected and all(x['present_for_at_least_90_percent'] for x in tested),
            'all_release_within_200ms': valid_trace and len(tested) == expected and all(x['released_within_200ms'] for x in tested),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--condition', required=True, choices=('voice_negative', 'office_negative', 'voice_tone', 'office_tone', 'offband'))
    parser.add_argument('--tone-hz', type=float)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--expected-appearances', type=int)
    parser.add_argument('--contrast-floor-db', type=float)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8')) if args.manifest else None
    if manifest is not None:
        entries = manifest.get('randomized_entries', manifest.get('files', []))
        if args.expected_appearances is None:
            args.expected_appearances = len(entries)
        if args.tone_hz is None and manifest.get('tone_hz') is not None:
            args.tone_hz = float(manifest['tone_hz'])
        if args.contrast_floor_db is None:
            args.contrast_floor_db = float(manifest.get('operating_contrast_target_db', 6.0))
    if args.expected_appearances is None:
        args.expected_appearances = 20
    if args.contrast_floor_db is None:
        args.contrast_floor_db = 6.0
    status = json.loads((run_dir / 'status.json').read_text(encoding='utf-8'))
    packets = read_packets(run_dir / 'packets.jsonl')
    capture = status.get('measurement', {}).get('final_capture', {})
    if status.get('raw_pcm'):
        capture_end = int(status['raw_pcm']['bytes']) // (4 * 4)
    else:
        capture_end = int(capture.get('frames_total', len(packets) * FRAMES_PER_PACKET))
    intervals, windows = event_intervals(packets, capture_end)
    realtime = summarize_realtime(status, packets, intervals)
    duration = float(status.get('measurement', {}).get('elapsed_seconds_before_stop', 0.0))
    report = {
        'phase': 8,
        'analysis': 'offline_only_no_live_capture_or_audio',
        'condition': args.condition,
        'source_run_status': status.get('status'),
        'realtime': realtime,
        'system_events': [{k: v for k, v in e.items() if k not in ('start_event', 'end_event')}
                          for e in intervals],
    }
    if args.condition.endswith('negative') or args.condition == 'offband':
        report['negative'] = negative_result(intervals, duration)
    else:
        if args.tone_hz is None:
            raise ValueError('--tone-hz is required for tone analysis')
        report['tone'] = tone_result(run_dir, status, packets, intervals, windows, args)
    report['realtime']['all_realtime_checks_pass'] = all((
        report['realtime']['zero_loss_overflow_clip'],
        report['realtime']['within_product_cpu_budget'],
        report['realtime']['within_total_packet_cadence'],
        not report['realtime']['sustained_queue_growth_over_10ms'],
        report['realtime']['base_restored'],
    ))
    if 'negative' in report:
        event_condition_pass = report['negative']['within_false_event_limit']
        if args.condition == 'offband':
            report['negative']['no_sustained_target_event'] = (
                report['negative']['false_sustained_events_ge_200ms'] == 0)
            event_condition_pass = report['negative']['no_sustained_target_event']
        report['acceptance_candidate_pass'] = (
            event_condition_pass and report['realtime']['all_realtime_checks_pass'])
    else:
        report['acceptance_candidate_pass'] = (
            report['tone']['pcm_trace_valid']
            and all(report['tone']['criteria'].values())
            and report['realtime']['all_realtime_checks_pass'])
    output = args.output.resolve() if args.output else run_dir / 'analysis.json'
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print('PHASE8_ACCEPTANCE_CANDIDATE=' + ('pass' if report['acceptance_candidate_pass'] else 'fail'))
    print('ANALYSIS=' + str(output))


if __name__ == '__main__':
    main()
