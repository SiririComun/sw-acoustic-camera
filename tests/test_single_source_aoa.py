"""Independent four-port plane-wave scenarios for the phase-10 PC prototype."""
from dataclasses import replace
import math

import numpy as np
import pytest

from pynq_acoustic_camera.single_source_aoa import AoAConfig, SingleSourceAoAEstimator


def plane_wave(azimuth_deg, elevation_deg, frequency_hz, *, config=None,
               sample_rate_hz=48000, frames=4096, snr_db=None, gains=None):
    """Generate signed-24 PCM in fixed PCM order from a geometric wavefront."""
    config = config or AoAConfig()
    azimuth, elevation = map(math.radians, (azimuth_deg, elevation_deg))
    direction = np.array((math.sin(azimuth) * math.cos(elevation),
                          math.sin(elevation),
                          math.cos(azimuth) * math.cos(elevation)))
    positions = np.asarray(config.positions_m)
    arrivals = positions @ direction / config.speed_of_sound_m_s
    time = np.arange(frames) / sample_rate_hz
    signal = 0.25 * np.cos(2 * math.pi * frequency_hz * (time[:, None] + arrivals[None, :]))
    if gains is not None:
        signal *= np.asarray(gains)[None, :]
    if snr_db is not None:
        rng = np.random.default_rng(4261)
        signal += rng.normal(0, 0.25 / (math.sqrt(2) * 10**(snr_db/20)), signal.shape)
    pcm = np.zeros((frames, 4), dtype=np.int32)
    for port, channel in enumerate(config.pcm_channels):
        pcm[:, channel] = np.rint(signal[:, port] * (1 << 23)).astype(np.int32)
    return pcm


@pytest.mark.parametrize('frequency_hz', [1000, 1877, 3000])
@pytest.mark.parametrize('azimuth,elevation', [(0, 0), (25, 0), (-25, 0),
                                                (0, 25), (0, -25), (35, 20),
                                                (-35, -20), (70, 0), (-70, 0),
                                                (0, 70), (0, -70)])
def test_known_directions_and_signs(frequency_hz, azimuth, elevation):
    estimator = SingleSourceAoAEstimator()
    pcm = plane_wave(azimuth, elevation, frequency_hz)
    result = estimator.estimate(pcm, sample_rate_hz=48000,
                                frequency_hz=frequency_hz, first_frame=8192,
                                window_index=2)
    assert result['status'] == 'VALID_PROVISIONAL', result
    assert abs(result['azimuth_deg'] - azimuth) < 1.0
    assert abs(result['elevation_deg'] - elevation) < 1.0
    assert result['first_frame'] == 8192
    assert result['window_index'] == 2
    assert result['geometry_provenance'] == 'reported_provisional'
    assert len(result['pair_phases_rad']) == 6
    assert all(abs(pair['residual_rad']) < 0.02 for pair in result['pair_phases_rad'])


def test_noise_and_relative_gain_do_not_change_synthetic_direction():
    pcm = plane_wave(22, -17, 1877, snr_db=20, gains=(0.6, 1.0, 1.2, 0.8))
    result = SingleSourceAoAEstimator().estimate(
        pcm, sample_rate_hz=48000, frequency_hz=1877)
    assert result['status'] == 'VALID_PROVISIONAL', result
    assert abs(result['azimuth_deg'] - 22) < 3.0
    assert abs(result['elevation_deg'] + 17) < 3.0


def test_reported_map_is_configurable_without_reordering_pcm():
    actual = replace(AoAConfig(), pcm_channels=(2, 0, 3, 1))
    pcm = plane_wave(28, -14, 2000, config=actual)
    before = pcm.copy()
    result = SingleSourceAoAEstimator(actual).estimate(
        pcm, sample_rate_hz=48000, frequency_hz=2000)
    assert result['status'] == 'VALID_PROVISIONAL', result
    assert abs(result['azimuth_deg'] - 28) < 1.0
    assert abs(result['elevation_deg'] + 14) < 1.0
    assert result['pcm_channels_by_port'] == [2, 0, 3, 1]
    np.testing.assert_array_equal(pcm, before)


def test_no_tone_and_missing_port_have_no_direction():
    estimator = SingleSourceAoAEstimator()
    silent = np.zeros((4096, 4), dtype=np.int32)
    silence = estimator.estimate(silent, sample_rate_hz=48000, frequency_hz=1200)
    assert silence['status'] == 'NO_TONE'
    assert silence['direction'] is None
    pcm = plane_wave(15, 10, 1200)
    pcm[:, 3] = 0
    missing = estimator.estimate(pcm, sample_rate_hz=48000, frequency_hz=1200)
    assert missing['status'] == 'INSUFFICIENT_CHANNELS'
    assert missing['azimuth_deg'] is None


def test_broadband_noise_without_tone_has_no_direction():
    rng = np.random.default_rng(2006)
    pcm = np.rint(rng.normal(0, 0.02, (4096, 4)) * (1 << 23)).astype(np.int32)
    result = SingleSourceAoAEstimator().estimate(
        pcm, sample_rate_hz=48000, frequency_hz=1877)
    assert result['status'] == 'NO_TONE'
    assert result['direction'] is None


def test_inconsistent_port_phase_has_no_direction():
    pcm = plane_wave(15, 10, 1877)
    shifted = np.roll(pcm[:, 3], 4)
    pcm[:, 3] = shifted
    result = SingleSourceAoAEstimator().estimate(
        pcm, sample_rate_hz=48000, frequency_hz=1877)
    assert result['status'] in ('INCONSISTENT_PAIRS', 'AMBIGUOUS_PHASE')
    assert result['direction'] is None


def test_spatially_aliased_geometry_reports_ambiguity():
    large = replace(AoAConfig(), positions_m=tuple(
        tuple(4 * value for value in port) for port in AoAConfig().positions_m))
    pcm = plane_wave(20, 0, 3000, config=large)
    result = SingleSourceAoAEstimator(large).estimate(
        pcm, sample_rate_hz=48000, frequency_hz=3000)
    assert result['status'] == 'AMBIGUOUS_PHASE', result
    assert result['direction'] is None


def test_non_coplanar_coordinates_and_actual_sample_rate():
    provisional = AoAConfig()
    offsets = (0.003, -0.002, 0.001, -0.001)
    positions = tuple((x, y, z) for (x, y, _), z in
                      zip(provisional.positions_m, offsets))
    config = replace(provisional, positions_m=positions)
    pcm = plane_wave(30, -15, 2000, config=config, sample_rate_hz=47900)
    result = SingleSourceAoAEstimator(config).estimate(
        pcm, sample_rate_hz=47900, frequency_hz=2000)
    assert result['status'] == 'VALID_PROVISIONAL', result
    assert abs(result['azimuth_deg'] - 30) < 1.0
    assert abs(result['elevation_deg'] + 15) < 1.0


def test_wrong_frequency_and_degenerate_geometry_do_not_return_direction():
    pcm = plane_wave(20, 0, 2000)
    result = SingleSourceAoAEstimator().estimate(
        pcm, sample_rate_hz=48000, frequency_hz=2500)
    assert result['status'] == 'NO_TONE'
    assert result['direction'] is None

    line = replace(AoAConfig(), positions_m=((0.0, 0.0, 0.0),
                                              (0.01, 0.0, 0.0),
                                              (0.02, 0.0, 0.0),
                                              (0.03, 0.0, 0.0)))
    line_pcm = plane_wave(20, 0, 2000, config=line)
    invalid = SingleSourceAoAEstimator(line).estimate(
        line_pcm, sample_rate_hz=48000, frequency_hz=2000)
    assert invalid['status'] == 'INVALID_GEOMETRY'
    assert invalid['direction'] is None


def test_wrong_sound_speed_can_bias_an_otherwise_valid_angle():
    pcm = plane_wave(35, 12, 2000)
    wrong = replace(AoAConfig(), speed_of_sound_m_s=390.0)
    result = SingleSourceAoAEstimator(wrong).estimate(
        pcm, sample_rate_hz=48000, frequency_hz=2000)
    assert result['status'] == 'VALID_PROVISIONAL'
    assert abs(result['azimuth_deg'] - 35) > 5.0


def test_invalid_pcm_and_map_are_rejected():
    with pytest.raises(ValueError, match='permutation'):
        AoAConfig(pcm_channels=(0, 1, 1, 3))
    estimator = SingleSourceAoAEstimator()
    with pytest.raises(ValueError, match='signed-24'):
        estimator.estimate(np.zeros((2048, 4), dtype=np.int16),
                           sample_rate_hz=48000, frequency_hz=2000)
    with pytest.raises(ValueError, match='sign-extended'):
        estimator.estimate(np.full((2048, 4), 1 << 23, dtype=np.int32),
                           sample_rate_hz=48000, frequency_hz=2000)
