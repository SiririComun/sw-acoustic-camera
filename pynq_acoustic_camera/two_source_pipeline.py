"""Host-safe PCM-to-two-tone-to-two-AoA link for synthetic development."""

import numpy as np

from .single_source_aoa import SingleSourceAoAEstimator
from .two_tone import StreamingTwoToneDetector


class TwoSourcePcmPipeline:
    """Consume continuous PCM packets and return per-source provisional AoA.

    Source IDs belong to the detector's active spectral tracks. No angle is
    carried into a later output row: skipped, rejected and absent estimates
    expose no direction. The camera runtime may later apply a bounded age.
    """

    def __init__(self, detector=None, estimator=None, *, aoa_update_stride=16,
                 max_fits_per_window=1):
        if type(aoa_update_stride) is not int or aoa_update_stride < 1:
            raise ValueError('aoa_update_stride must be a positive integer')
        if type(max_fits_per_window) is not int or max_fits_per_window not in (1, 2):
            raise ValueError('max_fits_per_window must be 1 or 2')
        self.detector = detector or StreamingTwoToneDetector()
        self.estimator = estimator or SingleSourceAoAEstimator()
        self.aoa_update_stride = aoa_update_stride
        self.max_fits_per_window = max_fits_per_window
        config = self.detector.config
        if config.window_frames < 512 or config.band_hz[0] < 1000 or config.band_hz[1] > 3000:
            raise ValueError('AoA requires windows >=512 and a band within 1..3 kHz')
        self.reset()

    def reset(self):
        self.detector.reset()
        self._history = np.empty((0, 4), dtype=np.int32)
        self._next_frame = None
        self._next_packet_index = None
        self._base_frame = None
        self._last_fit_window = {}

    @staticmethod
    def _absolute_window(relative, base_frame):
        window = dict(relative)
        window['first_frame'] += base_frame
        window['end_frame_exclusive'] += base_frame
        window['sources'] = [dict(source) for source in relative['sources']]
        for source in window['sources']:
            if source['start_frame'] is not None:
                source['start_frame'] += base_frame
        window['events'] = [dict(event) for event in relative['events']]
        for event in window['events']:
            event['reported_frame'] += base_frame
            if 'start_frame' in event:
                event['start_frame'] += base_frame
        return window

    def process_packet(self, packet):
        metadata = packet.metadata
        samples = np.asarray(packet.samples)
        required = ('packet_index', 'first_frame', 'frames', 'sample_rate_hz', 'source')
        if any(field not in metadata for field in required):
            raise ValueError('PCM packet metadata lacks required continuity fields')
        index, first, frames = (metadata[field] for field in required[:3])
        if (type(index) is not int or index < 0 or type(first) is not int or first < 0
                or type(frames) is not int or frames <= 0):
            raise ValueError('PCM packet counters must be nonnegative integers')
        if (samples.dtype != np.dtype('int32') or samples.shape != (frames, 4)
                or frames > self.detector.config.max_input_frames):
            raise ValueError('PCM packet shape, dtype or length is incompatible with detector')
        if metadata['source'] != 'microphones':
            raise ValueError('AoA pipeline requires microphone PCM, not a pattern source')
        if metadata['sample_rate_hz'] != self.detector.config.sample_rate_hz:
            raise ValueError('PCM and detector sample rates differ')
        if self._next_frame is not None and (first != self._next_frame
                                             or index != self._next_packet_index):
            self.reset()
            raise ValueError('PCM packet discontinuity; detector state reset')
        if self._base_frame is None:
            self._base_frame = first

        merged_start = first - len(self._history)
        merged = np.concatenate((self._history, samples), axis=0)
        results = []
        for relative in self.detector.process_block(samples):
            window = self._absolute_window(relative, self._base_frame)
            start = window['first_frame'] - merged_start
            stop = start + self.detector.config.window_frames
            if start < 0 or stop > len(merged):
                self.reset()
                raise RuntimeError('Detector window is unavailable in bounded PCM history')
            for event in window['events']:
                if event['type'] in ('start', 'end'):
                    self._last_fit_window.pop(event['id'], None)

            due = [source for source in window['sources']
                   if source['present'] and (source['id'] not in self._last_fit_window
                   or window['window_index'] - self._last_fit_window[source['id']]
                   >= self.aoa_update_stride)]
            # Sources without any prior fit go first, then the least recently
            # fitted source. This keeps the one-fit budget fair after startup.
            due.sort(key=lambda source: (self._last_fit_window.get(source['id'], -1),
                                         source['id']))
            selected = {source['id'] for source in due[:self.max_fits_per_window]}
            sources = []
            for source in window['sources']:
                source_id = source['id']
                aoa = None
                if source_id in selected:
                    aoa = self.estimator.estimate(
                        merged[start:stop],
                        sample_rate_hz=self.detector.config.sample_rate_hz,
                        frequency_hz=source['frequency_hz'],
                        first_frame=window['first_frame'],
                        window_index=window['window_index'])
                    self._last_fit_window[source_id] = window['window_index']
                sources.append({'id': source_id, 'present': source['present'],
                                'frequency_hz': source['frequency_hz'],
                                'aoa': aoa,
                                'aoa_skipped': source['present'] and aoa is None,
                                'direction': (aoa['direction'] if aoa is not None
                                              and aoa['status'] == 'VALID_PROVISIONAL'
                                              else None)})
            results.append({'packet_index': index, 'window': window,
                            'sources': sources})
        retained = self.detector.config.window_frames - 1
        self._history = np.array(merged[-retained:], dtype=np.int32, copy=True)
        self._next_frame = first + frames
        self._next_packet_index = index + 1
        return results
