"""Host-safe, bounded streaming detector for up to two PCM tones.

This is a digital prototype. A tone is a spectral component, not proof of a
separate physical source; harmonic rejection needs independent acoustic data.
"""

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class TwoToneConfig:
    sample_rate_hz: int = 48000
    band_hz: tuple = (1000.0, 3000.0)
    max_sources: int = 2  # Set to 1 for single-source automatic detection.
    manual_frequencies_hz: tuple = ()  # Empty selects automatic discovery.
    window_frames: int = 2048
    hop_frames: int = 1024
    max_input_frames: int = 4096
    min_separation_hz: float = 100.0
    manual_tolerance_hz: float = 50.0
    tracking_tolerance_hz: float = 60.0
    min_prominence_db: float = 15.0
    min_tone_level_dbfs: float = -65.0
    min_channels: int = 3
    enter_windows: int = 4
    release_windows: int = 2

    def __post_init__(self):
        if type(self.sample_rate_hz) is not int or self.sample_rate_hz <= 0:
            raise ValueError('sample_rate_hz must be positive')
        if (len(self.band_hz) != 2 or not all(math.isfinite(f) for f in self.band_hz)
                or not 0 < self.band_hz[0] < self.band_hz[1] < self.sample_rate_hz / 2):
            raise ValueError('band_hz must lie inside Nyquist')
        if type(self.max_sources) is not int or self.max_sources not in (1, 2):
            raise ValueError('max_sources must be 1 or 2')
        if len(self.manual_frequencies_hz) > self.max_sources or any(
                not math.isfinite(f) or not self.band_hz[0] <= f <= self.band_hz[1]
                for f in self.manual_frequencies_hz):
            raise ValueError('manual frequencies must fit max_sources and the band')
        if (len(self.manual_frequencies_hz) == 2 and abs(
                self.manual_frequencies_hz[0] - self.manual_frequencies_hz[1])
                < self.min_separation_hz):
            raise ValueError('manual frequencies are closer than min_separation_hz')
        for name in ('window_frames', 'hop_frames', 'max_input_frames',
                     'min_channels', 'enter_windows', 'release_windows'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError('{} must be positive'.format(name))
        if (self.hop_frames > self.window_frames or self.max_input_frames < self.hop_frames
                or self.min_channels > 4):
            raise ValueError('invalid window, packet, or channel count')
        for name in ('min_separation_hz', 'manual_tolerance_hz',
                     'tracking_tolerance_hz', 'min_prominence_db'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError('{} must be finite and positive'.format(name))
        if not math.isfinite(self.min_tone_level_dbfs):
            raise ValueError('min_tone_level_dbfs must be finite')


class StreamingTwoToneDetector:
    """Emit one window result per hop, preserving at most one PCM window.

    Automatic mode finds distinct local maxima in summed four-channel FFT
    power. Manual mode looks near each configured frequency. A component must
    pass level and prominence gates on at least ``min_channels`` PCM columns.
    IDs persist across windows until the component is released.
    """

    def __init__(self, config=None):
        self.config = config or TwoToneConfig()
        self._hann = np.hanning(self.config.window_frames)
        self._frequencies = np.fft.rfftfreq(
            self.config.window_frames, 1 / self.config.sample_rate_hz)
        self._band = np.flatnonzero((self._frequencies >= self.config.band_hz[0])
                                   & (self._frequencies <= self.config.band_hz[1]))
        if len(self._band) < 3:
            raise ValueError('analysis band contains fewer than three FFT bins')
        self._pending = np.empty((self.config.window_frames, 4), dtype=np.int32)
        self.reset()

    def reset(self):
        self._pending_frames = 0
        self._frames_seen = 0
        self._windows_seen = 0
        self._tracks = [self._new_track(i) for i in range(self.config.max_sources)]

    @staticmethod
    def _new_track(track_id):
        return {'id': track_id, 'present': False, 'frequency_hz': None,
                'streak': 0, 'misses': 0, 'start_frame': None,
                'supporting_channels': []}

    def process_block(self, samples):
        values = np.asarray(samples)
        if (values.dtype != np.dtype('int32') or values.ndim != 2
                or values.shape[1] != 4 or not 0 < len(values) <= self.config.max_input_frames):
            raise ValueError('expected bounded int32 PCM with shape (N,4)')
        if np.any(values < -(1 << 23)) or np.any(values >= (1 << 23)):
            raise ValueError('PCM values must be sign-extended signed24')
        results = []
        offset = 0
        while offset < len(values):
            count = min(self.config.window_frames - self._pending_frames,
                        len(values) - offset)
            self._pending[self._pending_frames:self._pending_frames + count] = values[offset:offset + count]
            self._pending_frames += count
            self._frames_seen += count
            offset += count
            if self._pending_frames == self.config.window_frames:
                results.append(self._analyze_window())
                self._windows_seen += 1
                retained = self.config.window_frames - self.config.hop_frames
                if retained:
                    self._pending[:retained] = self._pending[self.config.hop_frames:]
                self._pending_frames = retained
        return results

    def _candidate_bins(self, power):
        combined = power.sum(axis=1)
        if self.config.manual_frequencies_hz:
            bins = []
            for target in self.config.manual_frequencies_hz:
                neighborhood = self._band[np.abs(self._frequencies[self._band] - target)
                                          <= self.config.manual_tolerance_hz]
                bins.append(int(neighborhood[np.argmax(combined[neighborhood])])
                            if len(neighborhood) else None)
            return bins
        band = self._band
        peaks = [int(index) for index in band
                 if combined[index] > combined[index - 1]
                 and combined[index] >= combined[index + 1]]
        peaks.sort(key=lambda index: combined[index], reverse=True)
        chosen = []
        for index in peaks:
            if all(abs(self._frequencies[index] - self._frequencies[old])
                   >= self.config.min_separation_hz for old in chosen):
                chosen.append(index)
            if len(chosen) == 8:
                break
        return chosen

    def _candidate(self, index, power):
        if index is None:
            return None
        frequency = self._frequencies
        tiny = np.finfo(np.float64).tiny
        amplitudes = np.sqrt(np.max(power[max(0, index - 1):index + 2], axis=0))
        levels = 20 * np.log10(np.maximum(amplitudes / math.sqrt(2), tiny))
        floor_bins = self._band[np.abs(frequency[self._band] - frequency[index])
                                >= self.config.min_separation_hz]
        if len(floor_bins) == 0:
            return None
        floors = np.median(power[floor_bins], axis=0)
        prominences = 10 * np.log10(np.maximum(amplitudes ** 2, tiny)
                                    / np.maximum(floors, tiny))
        support = np.flatnonzero((levels >= self.config.min_tone_level_dbfs)
                                 & (prominences >= self.config.min_prominence_db)).tolist()
        if len(support) < self.config.min_channels:
            return None
        # Parabolic interpolation of the log power around the shared peak.
        combined = power[:, support].sum(axis=1)
        left, center, right = np.log(np.maximum(combined[index - 1:index + 2], tiny))
        curvature = left - 2 * center + right
        offset = 0.0 if curvature == 0 else np.clip(0.5 * (left - right) / curvature, -1, 1)
        estimate = float((index + offset) * self.config.sample_rate_hz
                         / self.config.window_frames)
        return {'frequency_hz': estimate, 'supporting_channels': support,
                'level_dbfs': float(np.median(levels[support])),
                'prominence_db': float(np.median(prominences[support]))}

    def _analyze_window(self):
        values = self._pending.astype(np.float64) / (1 << 23)
        values -= values.mean(axis=0, keepdims=True)
        spectrum = np.fft.rfft(values * self._hann[:, None], axis=0)
        power = np.abs(spectrum * (2 / self._hann.sum())) ** 2
        candidates = [self._candidate(index, power) for index in self._candidate_bins(power)]
        candidates = [candidate for candidate in candidates if candidate is not None]
        end = self._frames_seen
        assigned = {}
        used = set()
        if self.config.manual_frequencies_hz:
            # Manual source IDs always follow the configured frequency order.
            for track_id, target in enumerate(self.config.manual_frequencies_hz):
                for index, candidate in enumerate(candidates):
                    if (index not in used and abs(candidate['frequency_hz'] - target)
                            <= self.config.manual_tolerance_hz):
                        assigned[track_id] = candidate
                        used.add(index)
                        break
        else:
            for track in self._tracks:
                if track['frequency_hz'] is None:
                    continue
                matches = [(abs(candidate['frequency_hz'] - track['frequency_hz']), index)
                           for index, candidate in enumerate(candidates)
                           if index not in used]
                if matches and min(matches)[0] <= self.config.tracking_tolerance_hz:
                    index = min(matches)[1]
                    assigned[track['id']] = candidates[index]
                    used.add(index)
            for index, candidate in enumerate(candidates):
                if index in used:
                    continue
                free = next((track for track in self._tracks
                             if track['id'] not in assigned and track['frequency_hz'] is None), None)
                if free is None:
                    break
                assigned[free['id']] = candidate
                used.add(index)

        events = []
        for track in self._tracks:
            candidate = assigned.get(track['id'])
            if candidate is None:
                track['streak'] = 0
                if track['frequency_hz'] is not None:
                    track['misses'] += 1
                    if track['misses'] >= self.config.release_windows:
                        if track['present']:
                            events.append({'type': 'end', 'id': track['id'],
                                           'frequency_hz': track['frequency_hz'],
                                           'reported_frame': end})
                        track.update(self._new_track(track['id']))
                continue
            track['misses'] = 0
            track['supporting_channels'] = candidate['supporting_channels']
            if track['frequency_hz'] is None:
                track['frequency_hz'] = candidate['frequency_hz']
                track['streak'] = 1
                track['start_frame'] = end
            elif not track['present']:
                track['streak'] += 1
            if not track['present'] and track['streak'] >= self.config.enter_windows:
                track['present'] = True
                events.append({'type': 'start', 'id': track['id'],
                               'frequency_hz': track['frequency_hz'],
                               'start_frame': track['start_frame'],
                               'reported_frame': end})
        return {'window_index': self._windows_seen,
                'first_frame': end - self.config.window_frames,
                'end_frame_exclusive': end,
                'mode': 'manual' if self.config.manual_frequencies_hz else 'automatic',
                'candidates': candidates,
                'sources': [dict(track) for track in self._tracks],
                'events': events}
