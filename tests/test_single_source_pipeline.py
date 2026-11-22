"""Packet-level integration of continuous PCM, tone events and four-port AoA."""
import math

import numpy as np
import pytest

from pynq_acoustic_camera.continuous_capture import ContinuousPcmPacket
from pynq_acoustic_camera.single_source_aoa import AoAConfig
from pynq_acoustic_camera.single_source_pipeline import SingleSourcePcmPipeline


def make_packet(index, first_frame, frames=4096, *, tone=True, source='microphones'):
    config = AoAConfig()
    azimuth, elevation = math.radians(25), math.radians(-12)
    direction = np.array((math.sin(azimuth)*math.cos(elevation),
                          math.sin(elevation),
                          math.cos(azimuth)*math.cos(elevation)))
    positions = np.asarray(config.positions_m)
    phase = positions @ direction / config.speed_of_sound_m_s
    time = np.arange(first_frame, first_frame+frames) / 48000
    signal = (0.2 * np.cos(2*math.pi*1877*(time[:, None] + phase[None, :]))
              if tone else np.zeros((frames, 4)))
    pcm = np.rint(signal * (1 << 23)).astype(np.int32)
    return ContinuousPcmPacket(pcm, {
        'packet_index': index, 'first_frame': first_frame, 'frames': frames,
        'sample_rate_hz': 48000, 'source': source,
    })


def test_detector_event_feeds_matching_pcm_windows_to_aoa():
    pipeline = SingleSourcePcmPipeline()
    outputs = []
    packets = [make_packet(i, i*4096) for i in range(4)]
    untouched = [packet.samples.copy() for packet in packets]
    for packet in packets:
        outputs.extend(pipeline.process_packet(packet))
        assert len(pipeline._history) <= pipeline.detector.config.window_frames - 1
    assert any(row['window']['system']['event'] and
               row['window']['system']['event']['type'] == 'start'
               for row in outputs)
    active = [row for row in outputs if row['aoa'] is not None]
    assert active
    for row in active:
        result = row['aoa']
        assert result['status'] == 'VALID_PROVISIONAL', result
        assert abs(result['azimuth_deg'] - 25) < 2
        assert abs(result['elevation_deg'] + 12) < 2
        assert result['first_frame'] == row['window']['first_frame']
        assert result['window_index'] == row['window']['window_index']
        assert row['window']['end_frame_exclusive'] - row['window']['first_frame'] == 2048
    for packet, before in zip(packets, untouched):
        np.testing.assert_array_equal(packet.samples, before)


def test_aoa_stride_keeps_active_tone_windows_while_skipping_fits():
    pipeline = SingleSourcePcmPipeline(aoa_update_stride=4)
    outputs = []
    for index in range(6):
        outputs.extend(pipeline.process_packet(make_packet(index, index*4096)))
    computed = [row for row in outputs if row['aoa'] is not None]
    skipped = [row for row in outputs if row['aoa_skipped']]
    assert len(computed) >= 2
    assert skipped
    assert all(row['window']['system']['present'] for row in skipped)
    indices = [row['window']['window_index'] for row in computed]
    assert all(b - a >= 4 for a, b in zip(indices, indices[1:]))


def test_variable_packet_lengths_and_absolute_frame_offsets():
    pipeline = SingleSourcePcmPipeline()
    start = 10000
    sizes = (1536, 1024, 3072, 2048, 1024)
    outputs = []
    cursor = start
    for index, size in enumerate(sizes):
        outputs.extend(pipeline.process_packet(make_packet(index+20, cursor, size)))
        cursor += size
    windows = [row['window'] for row in outputs]
    assert windows[0]['first_frame'] == start
    assert all(b['first_frame'] - a['first_frame'] == 1024
               for a, b in zip(windows, windows[1:]))
    assert all(row['aoa']['first_frame'] == row['window']['first_frame']
               for row in outputs if row['aoa'] is not None)
    assert any(row['aoa'] is not None for row in outputs)
    event = next(row['window']['system']['event'] for row in outputs
                 if row['window']['system']['event'] is not None)
    assert event['start_frame'] >= start
    assert event['reported_frame'] >= event['start_frame']


def test_tone_release_stops_aoa_and_no_stale_angle_is_returned():
    pipeline = SingleSourcePcmPipeline()
    for index in range(3):
        pipeline.process_packet(make_packet(index, index*4096))
    outputs = []
    for index in range(3, 5):
        outputs.extend(pipeline.process_packet(make_packet(index, index*4096, tone=False)))
    assert any(row['window']['system']['event'] and
               row['window']['system']['event']['type'] == 'end' for row in outputs)
    assert any(row['aoa'] is not None and row['aoa']['status'] == 'NO_TONE'
               for row in outputs)
    assert all(row['aoa'] is None for row in outputs[-3:])


def test_packet_gap_resets_detector_before_new_segment():
    pipeline = SingleSourcePcmPipeline()
    pipeline.process_packet(make_packet(0, 0))
    with pytest.raises(ValueError, match='discontinuity'):
        pipeline.process_packet(make_packet(2, 8192))
    assert pipeline._next_frame is None
    fresh = pipeline.process_packet(make_packet(2, 8192))
    assert fresh[0]['window']['first_frame'] == 8192
    assert fresh[0]['aoa'] is None


def test_pattern_and_sample_rate_mismatch_are_rejected():
    pipeline = SingleSourcePcmPipeline()
    with pytest.raises(ValueError, match='microphone PCM'):
        pipeline.process_packet(make_packet(0, 0, source='pattern'))
    packet = make_packet(0, 0)
    packet.metadata['sample_rate_hz'] = 47900
    with pytest.raises(ValueError, match='sample rates differ'):
        pipeline.process_packet(packet)
