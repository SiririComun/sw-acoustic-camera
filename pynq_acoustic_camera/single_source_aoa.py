"""Provisional, host-safe four-channel tonal AoA from synchronous PCM."""
from dataclasses import dataclass
import math

import numpy as np


_PAIRS = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))
_SIGNED24_SCALE = float(1 << 23)


@dataclass(frozen=True)
class AoAConfig:
    """Port coordinates and channel identity are explicit, provisional inputs.

    Coordinates use metres: x right and y up when facing the camera, z out
    towards the observer. ``pcm_channels`` maps Mic1..Mic4 to PCM columns.
    """

    positions_m: tuple = ((-0.025, 0.025, 0.0),
                          (0.025, 0.025, 0.0),
                          (0.025, -0.025, 0.0),
                          (-0.025, -0.025, 0.0))
    pcm_channels: tuple = (0, 1, 2, 3)
    geometry_provenance: str = 'reported_provisional'
    speed_of_sound_m_s: float = 343.2
    min_tone_level_dbfs: float = -80.0
    min_prominence_db: float = 10.0
    max_phase_residual_rad: float = 0.25
    ambiguity_margin_rad: float = 0.05
    max_refinement_seeds: int = 16

    def __post_init__(self):
        positions = np.asarray(self.positions_m, dtype=np.float64)
        if positions.shape != (4, 3) or not np.isfinite(positions).all():
            raise ValueError('positions_m must contain four finite 3D port positions')
        if (len(self.pcm_channels) != 4
                or any(type(channel) is not int for channel in self.pcm_channels)
                or tuple(sorted(self.pcm_channels)) != (0, 1, 2, 3)):
            raise ValueError('pcm_channels must be a permutation of 0,1,2,3')
        if not self.geometry_provenance:
            raise ValueError('geometry_provenance must identify the source of positions')
        for name in ('speed_of_sound_m_s', 'min_tone_level_dbfs',
                     'min_prominence_db', 'max_phase_residual_rad',
                     'ambiguity_margin_rad'):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('{} must be finite'.format(name))
        if self.speed_of_sound_m_s <= 0 or self.min_prominence_db < 0:
            raise ValueError('Sound speed must be positive and prominence nonnegative')
        if type(self.max_refinement_seeds) is not int or self.max_refinement_seeds < 1:
            raise ValueError('max_refinement_seeds must be a positive integer')
        if not 0 < self.max_phase_residual_rad < math.pi or self.ambiguity_margin_rad < 0:
            raise ValueError('Phase thresholds are outside their valid ranges')


class SingleSourceAoAEstimator:
    """Fit a front-hemisphere plane-wave direction to four complex tone phases.

    This is a synthetic prototype. A valid result is not physical calibration.
    The bounded grid search compares circular phase residuals so diagonal
    baselines are never interpreted as unwrapped delays.
    """

    def __init__(self, config=None):
        self.config = config or AoAConfig()
        self._positions = np.asarray(self.config.positions_m, dtype=np.float64)

    @staticmethod
    def _direction_candidates(x_center=0.0, y_center=0.0, radius=1.0, step=0.05):
        xs = np.arange(max(-1.0, x_center - radius),
                       min(1.0, x_center + radius) + step / 2, step)
        ys = np.arange(max(-1.0, y_center - radius),
                       min(1.0, y_center + radius) + step / 2, step)
        x, y = np.meshgrid(xs, ys)
        x, y = x.ravel(), y.ravel()
        valid = x*x + y*y <= 1.0 + 1e-12
        x, y = x[valid], y[valid]
        return np.column_stack((x, y, np.sqrt(np.maximum(0.0, 1.0 - x*x - y*y))))

    def _scores(self, directions, observed, wave_number):
        delta = np.asarray([self._positions[j] - self._positions[i]
                            for i, j in _PAIRS])
        predicted = wave_number * directions @ delta.T
        residual = np.angle(np.exp(1j * (observed[None, :] - predicted)))
        return np.sqrt(np.mean(residual*residual, axis=1))

    def _fit(self, observed, wave_number):
        coarse = self._direction_candidates(step=0.05)
        scores = self._scores(coarse, observed, wave_number)
        seeds = []
        for index in np.argsort(scores):
            candidate = coarse[index]
            if all(np.linalg.norm(candidate[:2] - old[:2]) >= 0.15 for old in seeds):
                seeds.append(candidate)
                if len(seeds) == self.config.max_refinement_seeds:
                    break
        refined = []
        for seed in seeds:
            best = seed
            for radius, step in ((0.05, 0.005), (0.005, 0.0005)):
                candidates = self._direction_candidates(best[0], best[1], radius, step)
                index = int(np.argmin(self._scores(candidates, observed, wave_number)))
                best = candidates[index]
            refined.append((float(self._scores(best[None, :], observed, wave_number)[0]), best))
        refined.sort(key=lambda item: item[0])
        winner = refined[0]
        alternatives = [item for item in refined[1:]
                        if np.linalg.norm(item[1][:2] - winner[1][:2]) >= 0.15]
        return winner, alternatives[0] if alternatives else None

    def estimate(self, samples, *, sample_rate_hz, frequency_hz,
                 first_frame=None, window_index=None):
        """Return one direction or a reason why the window cannot support one."""
        values = np.asarray(samples)
        if values.dtype != np.dtype('int32') or values.ndim != 2 or values.shape[1] != 4 or len(values) < 512:
            raise ValueError('Expected at least 512 signed-24 int32 PCM frames (N,4)')
        if np.any(values < -(1 << 23)) or np.any(values >= (1 << 23)):
            raise ValueError('PCM values are not sign-extended signed24')
        if not isinstance(sample_rate_hz, (int, float)) or not math.isfinite(sample_rate_hz) or sample_rate_hz <= 6000:
            raise ValueError('sample_rate_hz must be finite and greater than 6000')
        if not isinstance(frequency_hz, (int, float)) or not math.isfinite(frequency_hz) or not 1000 <= frequency_hz <= 3000:
            raise ValueError('frequency_hz must be within 1000..3000 Hz')
        if first_frame is not None and (type(first_frame) is not int or first_frame < 0):
            raise ValueError('first_frame must be a nonnegative integer or None')
        if window_index is not None and (type(window_index) is not int or window_index < 0):
            raise ValueError('window_index must be a nonnegative integer or None')

        config = self.config
        x = values.astype(np.float64) / _SIGNED24_SCALE
        x = x[:, config.pcm_channels]
        x -= np.mean(x, axis=0)
        window = np.hanning(len(x))
        projection = window * np.exp(-2j * np.pi * frequency_hz * np.arange(len(x)) / sample_rate_hz)
        phasors = 2.0 * (projection @ x) / np.sum(window)
        rms = np.abs(phasors) / math.sqrt(2.0)
        tiny = np.finfo(float).tiny
        levels = 20.0 * np.log10(np.maximum(rms, tiny))
        spectrum = np.abs(np.fft.rfft(window[:, None] * x, axis=0))
        freqs = np.fft.rfftfreq(len(x), 1.0 / sample_rate_hz)
        floor_band = ((freqs >= 1000) & (freqs <= 3000)
                      & (np.abs(freqs - frequency_hz) >= 100))
        if not np.any(floor_band):
            raise ValueError('Window is too short to measure tone prominence')
        floor = 2.0 * np.median(spectrum[floor_band], axis=0) / np.sum(window) / math.sqrt(2.0)
        prominence = 20.0 * np.log10(np.maximum(rms, tiny) / np.maximum(floor, tiny))
        support = (levels >= config.min_tone_level_dbfs) & (prominence >= config.min_prominence_db)
        result = {
            'status': None,
            'direction': None,
            'azimuth_deg': None,
            'elevation_deg': None,
            'frequency_hz': float(frequency_hz),
            'sample_rate_hz': float(sample_rate_hz),
            'first_frame': first_frame,
            'window_index': window_index,
            'geometry_provenance': config.geometry_provenance,
            'pcm_channels_by_port': list(config.pcm_channels),
            'tone_level_dbfs_by_port': levels.tolist(),
            'prominence_db_by_port': prominence.tolist(),
            'support_by_port': support.tolist(),
            'pair_phases_rad': [],
            'phase_fit_rms_rad': None,
            'alternate_fit_rms_rad': None,
        }
        if not np.any(support):
            result['status'] = 'NO_TONE'
            return result
        if not np.all(support):
            result['status'] = 'INSUFFICIENT_CHANNELS'
            return result

        observed = np.array([np.angle(phasors[j] * np.conj(phasors[i]))
                             for i, j in _PAIRS])
        result['pair_phases_rad'] = [
            {'ports': [i + 1, j + 1], 'pcm_channels': [config.pcm_channels[i], config.pcm_channels[j]],
             'observed_rad': float(observed[k])}
            for k, (i, j) in enumerate(_PAIRS)]
        if np.linalg.matrix_rank(self._positions - self._positions.mean(axis=0), tol=1e-10) < 2:
            result['status'] = 'INVALID_GEOMETRY'
            return result
        wave_number = 2.0 * math.pi * frequency_hz / config.speed_of_sound_m_s
        winner, alternate = self._fit(observed, wave_number)
        score, direction = winner
        for pair, (i, j) in zip(result['pair_phases_rad'], _PAIRS):
            predicted = wave_number * np.dot(self._positions[j] - self._positions[i], direction)
            pair['predicted_rad'] = float(np.angle(np.exp(1j * predicted)))
            pair['residual_rad'] = float(np.angle(np.exp(1j * (pair['observed_rad'] - predicted))))
        result['phase_fit_rms_rad'] = score
        if alternate is not None:
            result['alternate_fit_rms_rad'] = alternate[0]
        if score > config.max_phase_residual_rad:
            result['status'] = 'INCONSISTENT_PAIRS'
            return result
        if alternate is not None and alternate[0] - score <= config.ambiguity_margin_rad:
            result['status'] = 'AMBIGUOUS_PHASE'
            return result
        result['status'] = 'VALID_PROVISIONAL'
        result['direction'] = direction.tolist()
        result['azimuth_deg'] = math.degrees(math.atan2(direction[0], direction[2]))
        result['elevation_deg'] = math.degrees(math.atan2(direction[1],
                                                         math.hypot(direction[0], direction[2])))
        return result
