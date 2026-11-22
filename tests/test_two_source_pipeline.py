"""Two independent synthetic wavefronts through PCM, detection and AoA."""

from dataclasses import replace
import math

import numpy as np
import pytest

from pynq_acoustic_camera.continuous_capture import ContinuousPcmPacket
from pynq_acoustic_camera.single_source_aoa import AoAConfig, SingleSourceAoAEstimator
from pynq_acoustic_camera.two_source_pipeline import TwoSourcePcmPipeline
from pynq_acoustic_camera.two_tone import StreamingTwoToneDetector, TwoToneConfig


WAVES = ((1550, 0.18, 25, -12), (2350, 0.12, -28, 15))


def packet(index, first_frame, frames=4096, waves=WAVES, *, config=None,
           bad_second_port=False, source='microphones'):
    config = config or AoAConfig()
    positions = np.asarray(config.positions_m)
    time = np.arange(first_frame, first_frame + frames) / 48000
    signals = np.zeros((frames, 4), dtype=np.float64)
    for wave_number, (frequency, amplitude, azimuth, elevation) in enumerate(waves):
        azimuth, elevation = map(math.radians, (azimuth, elevation))
        direction = np.array((math.sin(azimuth) * math.cos(elevation),
                              math.sin(elevation),
                              math.cos(azimuth) * math.cos(elevation)))
        phase = 2 * math.pi * frequency * positions @ direction / config.speed_of_sound_m_s
        if bad_second_port and wave_number == 1:
            phase[3] += 1.5
        signals += amplitude * np.cos(2 * np.pi * frequency * time[:, None]
                                      + phase[None, :])
    samples = np.zeros((frames, 4), dtype=np.int32)
    for port, channel in enumerate(config.pcm_channels):
        samples[:, channel] = np.rint(signals[:, port] * (1 << 23)).astype(np.int32)
    return ContinuousPcmPacket(samples, {
        'packet_index': index, 'first_frame': first_frame, 'frames': frames,
        'sample_rate_hz': 48000, 'source': source,
    })


def process(pipeline, waves_by_packet, *, config=None, start=0):
    rows = []
    for index, waves in enumerate(waves_by_packet):
        rows.extend(pipeline.process_packet(packet(index, start + index * 4096,
                                                   waves=waves, config=config)))
    return rows


def valid(row):
    return [source for source in row['sources'] if source['direction'] is not None]


def test_two_tones_get_independent_provisional_directions_in_same_window():
    pipeline = TwoSourcePcmPipeline(
        estimator=SingleSourceAoAEstimator(AoAConfig(max_refinement_seeds=4)),
        aoa_update_stride=1, max_fits_per_window=2)
    packets = [packet(i, 10000 + i * 4096) for i in range(3)]
    before = [item.samples.copy() for item in packets]
    rows = [row for item in packets for row in pipeline.process_packet(item)]
    both = [row for row in rows if len(valid(row)) == 2]
    assert both
    source_by_frequency = {round(source['frequency_hz'] / 100) * 100: source
                           for source in valid(both[-1])}
    for rounded, azimuth, elevation in ((1600, 25, -12), (2400, -28, 15)):
        source = source_by_frequency[rounded]
        assert source['aoa']['status'] == 'VALID_PROVISIONAL'
        assert abs(source['aoa']['azimuth_deg'] - azimuth) < 2
        assert abs(source['aoa']['elevation_deg'] - elevation) < 2
        assert source['aoa']['first_frame'] == both[-1]['window']['first_frame']
    assert both[-1]['window']['first_frame'] >= 10000
    assert len(pipeline._history) <= 2047
    for item, original in zip(packets, before):
        np.testing.assert_array_equal(item.samples, original)


def test_default_budget_alternates_two_fits_and_exposes_no_stale_direction():
    pipeline = TwoSourcePcmPipeline(
        estimator=SingleSourceAoAEstimator(AoAConfig(max_refinement_seeds=4)),
        aoa_update_stride=4)
    rows = process(pipeline, [WAVES] * 5)
    active = [row for row in rows if sum(source['present'] for source in row['sources']) == 2]
    assert active
    assert all(sum(source['aoa'] is not None for source in row['sources']) <= 1
               for row in active)
    assert {source['id'] for row in active for source in valid(row)} == {0, 1}
    assert all(source['direction'] is None for row in active for source in row['sources']
               if source['aoa_skipped'])


def test_manual_single_source_and_configurable_port_map():
    config = replace(AoAConfig(max_refinement_seeds=4),
                     pcm_channels=(2, 0, 3, 1))
    pipeline = TwoSourcePcmPipeline(
        detector=StreamingTwoToneDetector(TwoToneConfig(
            max_sources=1, manual_frequencies_hz=(2350,))),
        estimator=SingleSourceAoAEstimator(config), aoa_update_stride=1)
    rows = process(pipeline, [WAVES] * 3, config=config)
    directions = [source for row in rows for source in valid(row)]
    assert directions
    assert all(abs(source['frequency_hz'] - 2350) < 10 for source in directions)
    assert abs(directions[-1]['aoa']['azimuth_deg'] + 28) < 2
    assert abs(directions[-1]['aoa']['elevation_deg'] - 15) < 2
    assert directions[-1]['aoa']['pcm_channels_by_port'] == [2, 0, 3, 1]


def test_released_source_has_no_direction_while_other_remains():
    pipeline = TwoSourcePcmPipeline(
        estimator=SingleSourceAoAEstimator(AoAConfig(max_refinement_seeds=4)),
        aoa_update_stride=1)
    rows = process(pipeline, [WAVES] * 3 + [(WAVES[0],)] * 3 + [()] * 2)
    after_release = [row for row in rows if any(event['type'] == 'end'
                      and abs(event['frequency_hz'] - 2350) < 10
                      for event in row['window']['events'])]
    assert after_release
    released = after_release[0]
    assert len(valid(released)) <= 1
    assert all(source['direction'] is None for source in released['sources']
               if not source['present'])
    assert all(source['direction'] is None for source in rows[-1]['sources'])


def test_bad_phase_rejects_only_affected_tone():
    pipeline = TwoSourcePcmPipeline(
        estimator=SingleSourceAoAEstimator(AoAConfig(max_refinement_seeds=4)),
        aoa_update_stride=1, max_fits_per_window=2)
    rows = []
    for index in range(3):
        rows.extend(pipeline.process_packet(packet(index, index * 4096,
                                                   bad_second_port=True)))
    active = [row for row in rows if all(source['present'] for source in row['sources'])]
    assert active
    latest = active[-1]
    lower, upper = sorted(latest['sources'], key=lambda source: source['frequency_hz'])
    assert lower['aoa']['status'] == 'VALID_PROVISIONAL'
    assert lower['direction'] is not None
    assert upper['aoa']['status'] in ('INCONSISTENT_PAIRS', 'AMBIGUOUS_PHASE')
    assert upper['direction'] is None


def test_discontinuity_clears_tracks_before_new_segment():
    pipeline = TwoSourcePcmPipeline()
    process(pipeline, [WAVES] * 2)
    with pytest.raises(ValueError, match='discontinuity'):
        pipeline.process_packet(packet(3, 3 * 4096))
    assert pipeline._next_frame is None
    fresh = pipeline.process_packet(packet(3, 3 * 4096))
    assert fresh[0]['window']['first_frame'] == 3 * 4096
    assert all(not source['present'] and source['direction'] is None
               for source in fresh[0]['sources'])


def test_packet_validation_and_budget_validation():
    pipeline = TwoSourcePcmPipeline()
    with pytest.raises(ValueError, match='microphone PCM'):
        pipeline.process_packet(packet(0, 0, source='pattern'))
    wrong_rate = packet(0, 0)
    wrong_rate.metadata['sample_rate_hz'] = 47900
    with pytest.raises(ValueError, match='sample rates differ'):
        pipeline.process_packet(wrong_rate)
    with pytest.raises(ValueError, match='max_fits_per_window'):
        TwoSourcePcmPipeline(max_fits_per_window=3)
