"""Synthetic PCM checks for the Phase 12.1 detector, not acoustic acceptance."""

import numpy as np
import pytest

from pynq_acoustic_camera.two_tone import StreamingTwoToneDetector, TwoToneConfig


def pcm(first, frames=4096, tones=(), noise=0.001):
    rng = np.random.default_rng(first + 7)
    time = np.arange(first, first + frames) / 48000.0
    values = rng.normal(0, noise, (frames, 4))
    for frequency, amplitude in tones:
        phases = np.array([0.0, 0.2, -0.3, 0.45])
        values += amplitude * np.cos(2 * np.pi * frequency * time[:, None]
                                     + phases[None, :])
    return np.rint(np.clip(values, -0.999, 0.999) * (1 << 23)).astype(np.int32)


def feed(detector, segments):
    rows = []
    first = 0
    for tones in segments:
        rows.extend(detector.process_block(pcm(first, tones=tones)))
        first += 4096
    return rows


def active(row):
    return [source for source in row['sources'] if source['present']]


def test_automatic_zero_one_two_and_stable_ids():
    detector = StreamingTwoToneDetector()
    rows = feed(detector, [(), (), ((1550, 0.20),),
                           ((1550, 0.20), (2350, 0.12))] * 2)
    assert all(not active(row) for row in rows[:6])
    both = [row for row in rows if len(active(row)) == 2]
    assert both
    first_both = both[0]
    assert sorted(source['id'] for source in active(first_both)) == [0, 1]
    assert sorted(source['frequency_hz'] for source in active(first_both)) == pytest.approx(
        [1550, 2350], abs=10)
    for row in both:
        assert all(source['frequency_hz'] < 1900 if source['id'] == 0
                   else source['frequency_hz'] > 1900 for source in active(row))
    assert all(row['end_frame_exclusive'] - row['first_frame'] == 2048 for row in rows)
    assert all(b['first_frame'] - a['first_frame'] == 1024
               for a, b in zip(rows, rows[1:]))


def test_manual_one_and_two_sources_follow_configured_order():
    one = StreamingTwoToneDetector(TwoToneConfig(
        max_sources=1, manual_frequencies_hz=(2350,)))
    rows = feed(one, [((1550, 0.2), (2350, 0.12))] * 3)
    assert len(active(rows[-1])) == 1
    assert active(rows[-1])[0]['frequency_hz'] == pytest.approx(2350, abs=10)
    two = StreamingTwoToneDetector(TwoToneConfig(
        manual_frequencies_hz=(2350, 1550)))
    rows = feed(two, [((1550, 0.2), (2350, 0.12))] * 3)
    assert [source['id'] for source in active(rows[-1])] == [0, 1]
    assert [source['frequency_hz'] for source in active(rows[-1])] == pytest.approx(
        [2350, 1550], abs=10)


def test_automatic_single_source_mode_limits_output():
    detector = StreamingTwoToneDetector(TwoToneConfig(max_sources=1))
    rows = feed(detector, [((1550, 0.2), (2350, 0.12))] * 3)
    assert len(active(rows[-1])) == 1
    assert active(rows[-1])[0]['frequency_hz'] == pytest.approx(1550, abs=10)


def test_one_strong_tone_does_not_create_a_second_track():
    detector = StreamingTwoToneDetector()
    rows = feed(detector, [((1550, 0.7),)] * 4)
    assert all(len(active(row)) <= 1 for row in rows)
    assert len(active(rows[-1])) == 1


def test_release_does_not_keep_stale_tone_or_reassign_live_id():
    detector = StreamingTwoToneDetector()
    rows = feed(detector, [((1550, 0.2), (2350, 0.12))] * 3
                + [((2350, 0.12),)] * 3 + [()] * 2)
    assert len(active(rows[10])) == 2
    assert len(active(rows[15])) == 1
    assert active(rows[15])[0]['frequency_hz'] == pytest.approx(2350, abs=10)
    assert not active(rows[-1])
    starts = [event for row in rows for event in row['events'] if event['type'] == 'start']
    ends = [event for row in rows for event in row['events'] if event['type'] == 'end']
    assert len(starts) == len(ends) == 2
    assert {event['id'] for event in starts} == {event['id'] for event in ends}


def test_tone_outside_band_and_noise_do_not_create_sources():
    detector = StreamingTwoToneDetector()
    rows = feed(detector, [(), ((4000, 0.3),)] * 3)
    assert all(not active(row) for row in rows)
    assert not any(row['events'] for row in rows)


def test_close_tones_below_prototype_separation_are_not_claimed_as_two():
    detector = StreamingTwoToneDetector()
    rows = feed(detector, [((1550, 0.2), (1610, 0.12))] * 3)
    assert all(len(active(row)) <= 1 for row in rows)


def test_two_tones_above_prototype_separation_can_be_resolved():
    detector = StreamingTwoToneDetector()
    rows = feed(detector, [((1550, 0.2), (1670, 0.12))] * 3)
    assert sorted(source['frequency_hz'] for source in active(rows[-1])) == pytest.approx(
        [1550, 1670], abs=10)


def test_packet_boundaries_reset_and_invalid_pcm():
    detector = StreamingTwoToneDetector()
    rows = detector.process_block(pcm(0, 1536, ((1550, 0.2),)))
    assert not rows
    rows = detector.process_block(pcm(1536, 1024, ((1550, 0.2),)))
    assert rows[0]['first_frame'] == 0
    detector.reset()
    assert not detector.process_block(pcm(0, 1024, ((1550, 0.2),)))
    with pytest.raises(ValueError, match='bounded int32'):
        detector.process_block(np.ones((4097, 4), dtype=np.int32))
    with pytest.raises(ValueError, match='signed24'):
        detector.process_block(np.full((512, 4), 1 << 23, dtype=np.int32))


def test_invalid_mode_configuration():
    with pytest.raises(ValueError, match='max_sources'):
        TwoToneConfig(max_sources=3)
    with pytest.raises(ValueError, match='fit max_sources'):
        TwoToneConfig(max_sources=1, manual_frequencies_hz=(1550, 2350))
    with pytest.raises(ValueError, match='min_separation'):
        TwoToneConfig(manual_frequencies_hz=(1550, 1600))
