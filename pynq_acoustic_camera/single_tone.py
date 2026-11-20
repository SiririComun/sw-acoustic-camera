"""Incremental, bounded-memory single-tone detector for four-channel PCM."""
from dataclasses import dataclass
from collections import deque
import math

import numpy as np


@dataclass(frozen=True)
class ToneDetectorConfig:
    """Detector settings. Threshold defaults are provisional, not acceptance limits."""

    sample_rate_hz: int = 48000
    f_min_hz: float = 1000.0
    f_max_hz: float = 3000.0
    window_frames: int = 2048
    hop_frames: int = 1024
    max_input_frames: int = 4096
    noise_exclusion_hz: float = 100.0
    min_prominence_db: float = 15.0
    min_tone_level_dbfs: float = -120.0
    frequency_stability_hz: float = 75.0
    enter_windows: int = 2
    release_windows: int = 2
    system_min_channels: int = 3
    system_frequency_tolerance_hz: float = 6.0
    system_enter_windows: int = 4
    system_release_windows: int = 2
    system_track_prominence_db: float = 12.0

    def __post_init__(self):
        if type(self.sample_rate_hz) is not int or self.sample_rate_hz <= 0:
            raise ValueError('sample_rate_hz must be a positive integer')
        if not (math.isfinite(self.f_min_hz) and math.isfinite(self.f_max_hz)
                and 0 < self.f_min_hz < self.f_max_hz < self.sample_rate_hz / 2):
            raise ValueError('Require 0 < f_min_hz < f_max_hz < Nyquist')
        for name in ('window_frames', 'hop_frames', 'max_input_frames',
                     'enter_windows', 'release_windows',
                     'system_min_channels', 'system_enter_windows',
                     'system_release_windows'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError('{} must be a positive integer'.format(name))
        if self.hop_frames > self.window_frames:
            raise ValueError('hop_frames cannot exceed window_frames')
        if self.max_input_frames < self.hop_frames:
            raise ValueError('max_input_frames must be at least hop_frames')
        if self.system_min_channels > 4:
            raise ValueError('system_min_channels cannot exceed four channels')
        for name in ('noise_exclusion_hz', 'min_prominence_db',
                     'frequency_stability_hz', 'system_frequency_tolerance_hz',
                     'system_track_prominence_db'):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError('{} must be finite and non-negative'.format(name))
        if not math.isfinite(self.min_tone_level_dbfs):
            raise ValueError('min_tone_level_dbfs must be finite')


class SingleToneEventTracker:
    """Turn per-window channel candidates into one bounded system event stream.

    Three channels must agree in frequency for four consecutive analysis
    windows. This rejects changing speech harmonics seen in the 8.7 trace.
    The parameters remain provisional until independent physical acceptance.
    """

    def __init__(self, config=None):
        self.config = config or ToneDetectorConfig()
        self.reset()

    def reset(self):
        self._history = deque(maxlen=self.config.system_enter_windows)
        self._present = False
        self._frequency_hz = None
        self._start_frame = None
        self._misses = 0
        self._last_end_frame = None

    @property
    def active_frequency_hz(self):
        return self._frequency_hz if self._present else None

    def process_window(self, result):
        """Return system state and an optional start/end event for one window."""
        end_frame = result['end_frame_exclusive']
        if self._last_end_frame is not None and end_frame - self._last_end_frame != self.config.hop_frames:
            raise ValueError('System tracker requires contiguous analysis windows')
        self._last_end_frame = end_frame
        candidates = [c for c in result['channels'] if c['candidate']
                      and math.isfinite(c['estimated_frequency_hz'])]
        tolerance = self.config.system_frequency_tolerance_hz
        groups = [[c for c in candidates
                   if abs(c['estimated_frequency_hz'] - anchor['estimated_frequency_hz']) <= tolerance]
                  for anchor in candidates]
        best = max(groups, key=len, default=[])
        if len(best) >= self.config.system_min_channels:
            frequencies = sorted(c['estimated_frequency_hz'] for c in best)
            frequency = float(np.median(frequencies))
            supporting_channels = [c['channel'] for c in best]
        else:
            frequency = None
            supporting_channels = []

        event = None
        if self._present:
            locked_support = result.get('locked_support_channels', [])
            if ((frequency is not None and abs(frequency - self._frequency_hz) <= tolerance)
                    or len(locked_support) >= self.config.system_min_channels):
                self._misses = 0
            else:
                self._misses += 1
                if self._misses >= self.config.system_release_windows:
                    event = {'type': 'end', 'frequency_hz': self._frequency_hz,
                             'start_frame': self._start_frame, 'reported_frame': end_frame}
                    self._present = False
                    self._frequency_hz = None
                    self._start_frame = None
                    self._misses = 0
                    self._history.clear()

        if not self._present:
            self._history.append((end_frame, frequency))
            frequencies = [item[1] for item in self._history]
            if (len(frequencies) == self.config.system_enter_windows
                    and all(f is not None for f in frequencies)
                    and max(frequencies) - min(frequencies) <= tolerance):
                self._present = True
                self._frequency_hz = frequency
                self._start_frame = self._history[0][0]
                self._misses = 0
                event = {'type': 'start', 'frequency_hz': frequency,
                         'start_frame': self._start_frame, 'reported_frame': end_frame}
                self._history.clear()

        return {'present': self._present, 'frequency_hz': self._frequency_hz,
                'supporting_channels': supporting_channels,
                'locked_support_channels': result.get('locked_support_channels', []),
                'start_frame': self._start_frame, 'event': event}


class StreamingSingleToneDetector:
    """Process PCM packets incrementally and emit bounded per-window results.

    A Hann-windowed FFT selects the strongest peak in the configured band.
    Its prominence is measured against the median in-band bin power outside
    ``noise_exclusion_hz`` around that peak. Two stable candidate windows are
    required by default; two misses release a detection. The defaults are
    implementation starting points and must be tuned against the agreed
    noise and detection criteria before physical acceptance.

    A separate four-window, three-channel frequency consensus produces the
    ``system`` state and start/end events in each window result. Its event
    ``reported_frame`` marks when the decision became available; its
    ``start_frame`` is the first supporting window end, not a measured
    acoustic onset.

    Persistent sample storage is exactly one analysis window. Input packets
    larger than ``max_input_frames`` are rejected so accidental caller input
    cannot create an unbounded internal queue.
    """

    CHANNELS = 4
    SIGNED24_MIN = -(1 << 23)
    SIGNED24_MAX = (1 << 23) - 1

    def __init__(self, config=None):
        self.config = config or ToneDetectorConfig()
        self._window = np.hanning(self.config.window_frames).astype(np.float64)
        self._window_sum = float(self._window.sum())
        self._frequencies = np.fft.rfftfreq(
            self.config.window_frames, 1.0 / self.config.sample_rate_hz)
        self._band_indices = np.flatnonzero(
            (self._frequencies >= self.config.f_min_hz)
            & (self._frequencies <= self.config.f_max_hz))
        if len(self._band_indices) < 3:
            raise ValueError('Analysis window gives fewer than three bins in the selected band')
        self._pending = np.empty((self.config.window_frames, self.CHANNELS), dtype=np.int32)
        self.reset()

    def reset(self):
        """Clear overlap, sample counter, and per-channel detection history."""
        self._pending.fill(0)
        self._pending_frames = 0
        self._frames_seen = 0
        self._windows_seen = 0
        self._positive_streak = [0] * self.CHANNELS
        self._negative_streak = [0] * self.CHANNELS
        self._present = [False] * self.CHANNELS
        self._last_candidate_frequency = [None] * self.CHANNELS
        self._system = SingleToneEventTracker(self.config)
        self._locked_tracking_cache = None

    def process_block(self, samples):
        """Consume one signed-24-in-int32 ``(N,4)`` PCM block.

        Returns at most ``ceil(N / hop_frames)`` result dictionaries. Each
        result describes one analysis window and includes its end sample index.
        """
        values = np.asarray(samples)
        if values.dtype != np.dtype('int32') or values.ndim != 2 or values.shape[1] != self.CHANNELS:
            raise ValueError('Expected int32 PCM with shape (N,4)')
        if len(values) == 0 or len(values) > self.config.max_input_frames:
            raise ValueError('Input block length is outside the configured bounded size')
        if np.any(values < self.SIGNED24_MIN) or np.any(values > self.SIGNED24_MAX):
            raise ValueError('PCM values are not sign-extended signed24')

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
                result = self._analyze_window(self._frames_seen)
                results.append(result)
                self._windows_seen += 1
                retained = self.config.window_frames - self.config.hop_frames
                if retained:
                    self._pending[:retained] = self._pending[self.config.hop_frames:]
                self._pending_frames = retained
        return results

    def _analyze_window(self, end_frame):
        normalized = self._pending.astype(np.float64) / float(1 << 23)
        normalized -= normalized.mean(axis=0, keepdims=True)
        spectrum = np.fft.rfft(normalized * self._window[:, None], axis=0)
        amplitude = 2.0 * np.abs(spectrum) / self._window_sum
        power = amplitude * amplitude
        tiny = np.finfo(np.float64).tiny
        band_power = power[self._band_indices]
        peak_positions = np.argmax(band_power, axis=0)
        channels = []

        for channel, position in enumerate(peak_positions):
            peak_index = int(self._band_indices[position])
            peak_amplitude = float(amplitude[peak_index, channel])
            peak_power = float(power[peak_index, channel])
            peak_frequency = self._interpolated_frequency(peak_index, power[:, channel])
            floor_mask = (self._frequencies[self._band_indices]
                          - self._frequencies[peak_index]) > self.config.noise_exclusion_hz
            floor_mask |= (self._frequencies[peak_index]
                           - self._frequencies[self._band_indices]) > self.config.noise_exclusion_hz
            floor_bins = self._band_indices[floor_mask]
            if len(floor_bins):
                floor_power = float(np.median(power[floor_bins, channel]))
                prominence_db = 10.0 * math.log10(max(peak_power, tiny) / max(floor_power, tiny))
                noise_floor_dbfs = 10.0 * math.log10(max(floor_power, tiny))
            else:
                floor_power = None
                prominence_db = None
                noise_floor_dbfs = None
            tone_level_dbfs = (20.0 * math.log10(max(peak_amplitude / math.sqrt(2.0), tiny)))
            candidate = bool(
                prominence_db is not None
                and prominence_db >= self.config.min_prominence_db
                and tone_level_dbfs >= self.config.min_tone_level_dbfs)
            stable_candidate = candidate and (
                self._last_candidate_frequency[channel] is None
                or abs(peak_frequency - self._last_candidate_frequency[channel])
                <= self.config.frequency_stability_hz)

            if stable_candidate:
                self._positive_streak[channel] += 1
                self._negative_streak[channel] = 0
                self._last_candidate_frequency[channel] = peak_frequency
                if self._positive_streak[channel] >= self.config.enter_windows:
                    self._present[channel] = True
            else:
                self._positive_streak[channel] = 0
                self._negative_streak[channel] += 1
                self._last_candidate_frequency[channel] = None
                if self._negative_streak[channel] >= self.config.release_windows:
                    self._present[channel] = False

            channels.append({
                'channel': channel,
                'present': self._present[channel],
                'candidate': candidate,
                'estimated_frequency_hz': peak_frequency,
                'tone_level_dbfs': tone_level_dbfs,
                'local_noise_floor_dbfs_per_bin': noise_floor_dbfs,
                'confidence_db': prominence_db,
                'positive_streak': self._positive_streak[channel],
            })

        result = {
            'window_index': self._windows_seen,
            'first_frame': end_frame - self.config.window_frames,
            'end_frame_exclusive': end_frame,
            'window_frames': self.config.window_frames,
            'hop_frames': self.config.hop_frames,
            'band_hz': [self.config.f_min_hz, self.config.f_max_hz],
            'channels': channels,
        }
        locked_frequency = self._system.active_frequency_hz
        if locked_frequency is not None:
            if (self._locked_tracking_cache is None
                    or self._locked_tracking_cache[0] != locked_frequency):
                peak_bin = int(np.argmin(np.abs(self._frequencies - locked_frequency)))
                distance = np.abs(self._frequencies[self._band_indices] - locked_frequency)
                neighborhood = self._band_indices[(distance >= 80.0) & (distance <= 300.0)]
                self._locked_tracking_cache = (locked_frequency, peak_bin, neighborhood)
            _, peak_bin, neighborhood = self._locked_tracking_cache
            support = []
            peaks = np.max(power[max(0, peak_bin - 1):peak_bin + 2, :], axis=0)
            floors = np.median(power[neighborhood, :], axis=0) if len(neighborhood) else None
            for channel in range(self.CHANNELS):
                peak = float(peaks[channel])
                floor = float(floors[channel]) if floors is not None else 0.0
                prominence = (10.0 * math.log10(max(peak, tiny) / max(floor, tiny))
                              if len(neighborhood) else None)
                channels[channel]['locked_tone_prominence_db'] = prominence
                if prominence is not None and prominence >= self.config.system_track_prominence_db:
                    support.append(channel)
            result['locked_support_channels'] = support
        result['system'] = self._system.process_window(result)
        return result

    def _interpolated_frequency(self, peak_index, power):
        bin_hz = self.config.sample_rate_hz / self.config.window_frames
        if peak_index <= 0 or peak_index >= len(power) - 1:
            return float(self._frequencies[peak_index])
        left, center, right = (math.log(max(float(power[index]), np.finfo(float).tiny))
                               for index in (peak_index - 1, peak_index, peak_index + 1))
        denominator = left - 2.0 * center + right
        if denominator == 0.0:
            offset = 0.0
        else:
            offset = max(-1.0, min(1.0, 0.5 * (left - right) / denominator))
        return float((peak_index + offset) * bin_hz)
