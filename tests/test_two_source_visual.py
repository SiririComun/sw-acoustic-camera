"""PC checks for provisional, independent and combined source layers."""

import numpy as np
import pytest

from pynq_acoustic_camera.two_source_visual import (
    TwoSourceVisualState, direction_pixel, render_two_source_maps,
)
from tools.phase12_live_camera import selected_frame


def estimate(source_id, azimuth, elevation, frequency):
    return {'id': source_id, 'frequency_hz': frequency,
            'azimuth_deg': azimuth, 'elevation_deg': elevation,
            'status': 'VALID_PROVISIONAL', 'first_frame': 0}


def row(source_id, aoa, *, present=True, skipped=False, events=()):
    return {'window': {'events': list(events)}, 'sources': [{
        'id': source_id, 'present': present,
        'frequency_hz': 1550.0 + source_id * 800,
        'aoa': aoa, 'aoa_skipped': skipped,
    }]}


def aoa(azimuth=0, elevation=0, *, status='VALID_PROVISIONAL'):
    return {'status': status, 'direction': [0, 0, 1] if status == 'VALID_PROVISIONAL' else None,
            'azimuth_deg': azimuth, 'elevation_deg': elevation, 'first_frame': 0}


def test_two_independent_layers_and_combined_frame_keep_input_unchanged():
    frame = np.full((120, 160, 3), 40, dtype=np.uint8)
    before = frame.copy()
    result = render_two_source_maps(frame, [estimate(0, 20, -10, 1550),
                                            estimate(1, -20, 10, 2350)],
                                    sigma_px=8)
    assert set(result['source_maps']) == {0, 1}
    assert set(result['source_frames']) == {0, 1}
    pixels = {item['id']: item['pixel'] for item in result['visible_sources']}
    assert pixels[0] != pixels[1]
    for source_id, (x, y) in pixels.items():
        assert result['source_maps'][source_id][y, x] == 1
        assert result['combined_map'][y, x] == 1
        assert not np.array_equal(result['source_frames'][source_id][y, x], frame[y, x])
        other = 1 - source_id
        np.testing.assert_array_equal(result['source_frames'][other][y, x], frame[y, x])
    np.testing.assert_array_equal(frame, before)
    assert not np.array_equal(result['combined_frame'], frame)


def test_empty_rejected_and_off_frame_sources_make_no_map():
    frame = np.full((120, 160, 3), 40, dtype=np.uint8)
    for estimates in ([], [dict(estimate(0, 0, 0, 1550), status='NO_TONE')],
                      [estimate(1, 75, 0, 2350)]):
        result = render_two_source_maps(frame, estimates)
        assert result['visible_sources'] == []
        assert not result['source_maps']
        np.testing.assert_array_equal(result['combined_frame'], frame)
    result = render_two_source_maps(frame, [estimate(0, 0, 0, 1550)],
                                    include_individual=False)
    assert result['source_frames'] == {}
    assert len(result['visible_sources']) == 1


def test_freshness_skip_rejection_release_and_discontinuity_clear():
    state = TwoSourceVisualState(max_age_s=0.5)
    state.update(row(0, aoa(20, 0)), 10.0)
    state.update(row(1, aoa(-20, 0)), 10.1)
    assert [source['id'] for source in state.snapshot(10.2)] == [0, 1]
    state.update(row(0, None, skipped=True), 10.3)
    assert [source['id'] for source in state.snapshot(10.4)] == [0, 1]
    state.update(row(1, aoa(status='INCONSISTENT_PAIRS')), 10.4)
    assert [source['id'] for source in state.snapshot(10.4)] == [0]
    assert state.snapshot(10.51) == []
    state.update(row(0, aoa()), 11.0)
    state.update(row(0, None, present=False, events=[{'type': 'end', 'id': 0}]), 11.1)
    assert state.snapshot(11.1) == []
    state.update(row(0, aoa()), 12.0)
    state.clear()  # The PCM worker calls this on a discontinuity.
    assert state.snapshot(12.0) == []


def test_projection_matches_phase11_convention():
    assert direction_pixel(0, 0, 640, 480) == (320, 240)
    assert direction_pixel(20, 0, 640, 480)[0] < 320
    assert direction_pixel(0, 20, 640, 480)[1] < 240


def test_prepared_runtime_selects_combined_or_individual_view():
    frame = np.full((120, 160, 3), 40, dtype=np.uint8)
    rendered = render_two_source_maps(frame, [estimate(0, 0, 0, 1550),
                                              estimate(1, -20, 10, 2350)])
    np.testing.assert_array_equal(selected_frame(frame, rendered, 'combined'),
                                  rendered['combined_frame'])
    np.testing.assert_array_equal(selected_frame(frame, rendered, 'source0'),
                                  rendered['source_frames'][0])
    np.testing.assert_array_equal(selected_frame(frame, rendered, 'source1'),
                                  rendered['source_frames'][1])
    empty = render_two_source_maps(frame, [])
    np.testing.assert_array_equal(selected_frame(frame, empty, 'source1'), frame)


def test_live_sparse_render_matches_exported_combined_pixels():
    frame = np.full((480, 640, 3), 40, dtype=np.uint8)
    sources = [estimate(0, 25, -12, 1550), estimate(1, -28, 15, 2350)]
    full = render_two_source_maps(frame, sources)
    live = render_two_source_maps(frame, sources, include_individual=False,
                                  include_maps=False)
    assert live['source_maps'] == {}
    assert live['combined_map'] is None
    np.testing.assert_array_equal(live['combined_frame'], full['combined_frame'])


def test_overlapping_sources_contribute_to_combined_view():
    frame = np.full((120, 160, 3), 40, dtype=np.uint8)
    rendered = render_two_source_maps(frame, [estimate(0, 0, 0, 1550),
                                              estimate(1, 0, 0, 2350)])
    x, y = direction_pixel(0, 0, 160, 120)
    pixel = rendered['combined_frame'][y, x]
    assert pixel[0] > frame[y, x, 0]  # Blue contribution from source 1.
    assert pixel[2] > frame[y, x, 2]  # Red contribution from source 0.
    assert not np.array_equal(pixel, rendered['source_frames'][0][y, x])
    assert not np.array_equal(pixel, rendered['source_frames'][1][y, x])


def test_invalid_frames_and_render_settings_are_rejected():
    with pytest.raises(ValueError, match='uint8'):
        render_two_source_maps(np.zeros((10, 10), dtype=np.uint8), [])
    with pytest.raises(ValueError, match='sigma_px'):
        render_two_source_maps(np.zeros((10, 10, 3), dtype=np.uint8), [], sigma_px=0)
    with pytest.raises(ValueError, match='distinct'):
        render_two_source_maps(np.zeros((10, 10, 3), dtype=np.uint8),
                               [estimate(0, 0, 0, 1550)] * 2)
    with pytest.raises(ValueError, match='max_age_s'):
        TwoSourceVisualState(max_age_s=0)
