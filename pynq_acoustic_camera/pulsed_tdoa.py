"""Provisional four-microphone AoA from the onset of one acoustic pulse.

The estimator measures matched narrowband-envelope threshold frames and fits
their six pairwise differences to a plane wave. The common envelope-filter and
buzzer turn-on delays cancel in those differences. These are TDoA AoA
candidates, not absolute ToA range measurements or physical calibration.
"""
from dataclasses import dataclass
import math
from typing import Optional

import numpy as np

from .single_source_aoa import AoAConfig, _PAIRS, _SIGNED24_SCALE


@dataclass(frozen=True)
class PulseToAConfig:
    """Signal thresholds for the four-channel pulsed-arrival estimator."""

    aoa: Optional[AoAConfig] = None
    envelope_cycles: float = 3.0
    threshold_ratio: float = 0.5
    min_snr_db: float = 6.0

    def __post_init__(self):
        if self.aoa is not None and not isinstance(self.aoa, AoAConfig):
            raise TypeError('aoa must be an AoAConfig or None')
        for name in ('envelope_cycles', 'threshold_ratio', 'min_snr_db'):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise ValueError('{} must be finite'.format(name))
        if self.envelope_cycles < 1:
            raise ValueError('envelope_cycles must be at least one')
        if not 0 < self.threshold_ratio < 1 or self.min_snr_db < 0:
            raise ValueError('threshold_ratio must be in (0,1) and min_snr_db nonnegative')


class PulsedTdoAAoAEstimator:
    """Estimate front-hemisphere direction from one recorded buzzer burst.

    Pass the original PCM frame order unchanged. ``AoAConfig.pcm_channels``
    identifies which PCM column belongs to each physical microphone. Supply
    ``baseline_frames`` as the number of pre-pulse frames in the recording.
    """

    def __init__(self, config=None):
        self.config = config or PulseToAConfig()
        self.aoa = self.config.aoa or AoAConfig()
        self._positions = np.asarray(self.aoa.positions_m, dtype=np.float64)

    @staticmethod
    def _measure_channel(samples, sample_rate_hz, frequency_hz, window_frames,
                         baseline_frames, threshold_ratio, min_snr_db):
        values = np.asarray(samples, dtype=np.float64)
        baseline = float(np.mean(values[:baseline_frames]))
        values = values - baseline
        window = np.hanning(window_frames)
        oscillator = np.exp(-2j * np.pi * frequency_hz
                            * np.arange(window_frames, dtype=np.float64) / sample_rate_hz)
        kernel = window * oscillator
        projection = np.convolve(values, kernel[::-1], mode='valid')
        envelope = 2.0 * np.abs(projection) / max(float(np.sum(window)), 1e-15)
        baseline_envelopes = max(1, baseline_frames - window_frames + 1)
        noise = float(np.median(envelope[:baseline_envelopes]))
        search_start = baseline_envelopes
        if search_start >= len(envelope):
            return {'detected': False, 'reason': 'NO_POST_TRIGGER_FRAMES'}
        peak_index = search_start + int(np.argmax(envelope[search_start:]))
        peak = float(envelope[peak_index])
        floor = max(noise, np.finfo(np.float64).tiny)
        snr_db = 20.0 * math.log10(max(peak, floor) / floor)
        if peak <= 0 or snr_db < min_snr_db:
            return {'detected': False, 'reason': 'LOW_SNR', 'peak': peak,
                    'noise': noise, 'snr_db': snr_db}
        threshold = max(4.0 * noise, threshold_ratio * peak)
        crossings = np.flatnonzero(envelope[search_start:] >= threshold)
        if crossings.size == 0:
            return {'detected': False, 'reason': 'NO_THRESHOLD_CROSSING', 'peak': peak,
                    'noise': noise, 'snr_db': snr_db}
        index = search_start + int(crossings[0])
        fraction = 0.0
        if index > search_start:
            previous = float(envelope[index - 1])
            current = float(envelope[index])
            if current > previous:
                fraction = min(1.0, max(0.0, (threshold - previous) / (current - previous)))
        frame = index - 1 + fraction + (window_frames - 1) / 2.0
        return {'detected': True, 'arrival_frame': float(frame), 'peak': peak,
                'noise': noise, 'snr_db': snr_db, 'threshold': threshold,
                'envelope_window_frames': window_frames}

    def estimate(self, samples, *, sample_rate_hz, frequency_hz,
                 baseline_frames, first_frame=0):
        """Return four onset estimates, six pair delays, and a direction candidate."""
        values = np.asarray(samples)
        if values.dtype != np.dtype('int32') or values.ndim != 2 or values.shape[1] != 4:
            raise ValueError('Expected original four-channel signed-24 PCM with shape (N,4)')
        if len(values) < 64 or np.any(values < -(1 << 23)) or np.any(values >= (1 << 23)):
            raise ValueError('PCM must contain at least 64 sign-extended signed-24 frames')
        if (isinstance(sample_rate_hz, bool) or not isinstance(sample_rate_hz, (int, float))
                or not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0):
            raise ValueError('sample_rate_hz must be finite and positive')
        if (isinstance(frequency_hz, bool) or not isinstance(frequency_hz, (int, float))
                or not math.isfinite(frequency_hz) or not 0 < frequency_hz < sample_rate_hz / 2):
            raise ValueError('frequency_hz must lie below the PCM Nyquist frequency')
        if type(baseline_frames) is not int or not 1 <= baseline_frames < len(values):
            raise ValueError('baseline_frames must identify a nonempty pre-pulse segment')
        if type(first_frame) is not int or first_frame < 0:
            raise ValueError('first_frame must be a nonnegative integer')

        window_frames = max(16, int(round(sample_rate_hz * self.config.envelope_cycles
                                          / frequency_hz)))
        if baseline_frames < window_frames or len(values) - baseline_frames < 1:
            raise ValueError('Need at least one full envelope window before the pulse')

        ordered_channels = values[:, self.aoa.pcm_channels].astype(np.float64) / _SIGNED24_SCALE
        arrivals = []
        for mic in range(4):
            row = self._measure_channel(
                ordered_channels[:, mic], sample_rate_hz, frequency_hz, window_frames,
                baseline_frames, self.config.threshold_ratio, self.config.min_snr_db)
            if row.get('detected'):
                row['mic'] = mic + 1
                row['pcm_channel'] = self.aoa.pcm_channels[mic]
                row['arrival_frame'] += first_frame
            arrivals.append(row)

        result = {
            'status': 'INSUFFICIENT_CHANNELS',
            'frequency_hz': float(frequency_hz),
            'sample_rate_hz': float(sample_rate_hz),
            'baseline_frames': baseline_frames,
            'first_frame': first_frame,
            'geometry_provenance': self.aoa.geometry_provenance,
            'pcm_channels_by_mic': list(self.aoa.pcm_channels),
            'arrival_by_mic': arrivals,
            'pairwise_tdoa': [],
            'direction': None,
            'azimuth_deg': None,
            'elevation_deg': None,
            'fit_rms_us': None,
        }
        if not all(row.get('detected') for row in arrivals):
            result['status'] = ('NO_PULSE' if not any(row.get('detected') for row in arrivals)
                                else 'INSUFFICIENT_CHANNELS')
            return result

        arrival_frames = np.asarray([row['arrival_frame'] for row in arrivals])
        pair_rows = []
        matrix = []
        targets = []
        for mic_i, mic_j in _PAIRS:
            dt = (arrival_frames[mic_j] - arrival_frames[mic_i]) / sample_rate_hz
            baseline = self._positions[mic_j] - self._positions[mic_i]
            pair_rows.append({
                'mics': [mic_i + 1, mic_j + 1],
                'pcm_channels': [self.aoa.pcm_channels[mic_i], self.aoa.pcm_channels[mic_j]],
                'delta_frames': float(arrival_frames[mic_j] - arrival_frames[mic_i]),
                'delta_us': float(dt * 1e6),
            })
            matrix.append(baseline)
            targets.append(-self.aoa.speed_of_sound_m_s * dt)
        result['pairwise_tdoa'] = pair_rows
        matrix = np.asarray(matrix, dtype=np.float64)
        targets = np.asarray(targets, dtype=np.float64)
        centered = self._positions - self._positions.mean(axis=0)
        geometry_rank = int(np.linalg.matrix_rank(centered, tol=1e-10))
        if geometry_rank < 2:
            result['status'] = 'INVALID_GEOMETRY'
            return result

        projected, _, _, _ = np.linalg.lstsq(matrix, targets, rcond=None)
        if geometry_rank == 2:
            _, _, vectors = np.linalg.svd(centered, full_matrices=True)
            normal = vectors[-1]
            if normal[2] < 0:
                normal = -normal
            in_plane_norm = float(np.linalg.norm(projected))
            if in_plane_norm > 1.0:
                result['status'] = 'INCONSISTENT_TDOA'
                return result
            direction = projected + normal * math.sqrt(max(0.0, 1.0 - in_plane_norm ** 2))
        else:
            magnitude = float(np.linalg.norm(projected))
            if magnitude <= np.finfo(np.float64).tiny:
                result['status'] = 'INCONSISTENT_TDOA'
                return result
            direction = projected / magnitude

        for pair, (mic_i, mic_j) in zip(pair_rows, _PAIRS):
            predicted = -float(np.dot(self._positions[mic_j] - self._positions[mic_i], direction))
            predicted /= self.aoa.speed_of_sound_m_s
            pair['predicted_delta_us'] = predicted * 1e6
            pair['residual_us'] = pair['delta_us'] - pair['predicted_delta_us']
        rms_us = math.sqrt(float(np.mean([row['residual_us'] ** 2 for row in pair_rows])))
        result.update({
            'status': 'TDOA_DIRECTION_CANDIDATE',
            'direction': direction.tolist(),
            'azimuth_deg': math.degrees(math.atan2(direction[0], direction[2])),
            'elevation_deg': math.degrees(math.atan2(
                direction[1], math.hypot(direction[0], direction[2]))),
            'fit_rms_us': rms_us,
        })
        return result
