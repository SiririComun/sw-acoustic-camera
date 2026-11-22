"""Host-safe link from continuous PCM packets to tone detection and AoA."""
import numpy as np

from .single_source_aoa import SingleSourceAoAEstimator
from .single_tone import StreamingSingleToneDetector


class SingleSourcePcmPipeline:
    """Consume ordered PCM packets and emit detector/AoA windows.

    Only the last ``window_frames - 1`` samples are retained between packets.
    A continuity error resets the detector before raising; the caller must
    treat the interrupted segment as invalid and may start a new one.
    """

    def __init__(self, detector=None, estimator=None, aoa_update_stride=1):
        if type(aoa_update_stride) is not int or aoa_update_stride < 1:
            raise ValueError('aoa_update_stride must be a positive integer')
        self.detector = detector or StreamingSingleToneDetector()
        self.estimator = estimator or SingleSourceAoAEstimator()
        self.aoa_update_stride = aoa_update_stride
        config = self.detector.config
        if config.window_frames < 512:
            raise ValueError('AoA requires a detector window of at least 512 frames')
        if config.f_min_hz < 1000 or config.f_max_hz > 3000:
            raise ValueError('Detector band must be contained in the AoA 1..3 kHz band')
        self.reset()

    def reset(self):
        """Start an independent segment with no prior detector or PCM state."""
        self.detector.reset()
        self._history = np.empty((0, 4), dtype=np.int32)
        self._next_frame = None
        self._next_packet_index = None
        self._base_frame = None
        self._last_aoa_window = None

    @staticmethod
    def _absolute_window(window, base_frame):
        result = dict(window)
        result['first_frame'] += base_frame
        result['end_frame_exclusive'] += base_frame
        system = dict(result['system'])
        if system['start_frame'] is not None:
            system['start_frame'] += base_frame
        if system['event'] is not None:
            event = dict(system['event'])
            event['start_frame'] += base_frame
            event['reported_frame'] += base_frame
            system['event'] = event
        result['system'] = system
        return result

    def process_packet(self, packet):
        """Process a ContinuousPcmPacket or equivalent detached host packet.

        Output has absolute capture frame indices. ``aoa`` is ``None`` until
        the system detector reports an active tone; an active window can still
        contain an AoA rejection status and no direction.
        """
        metadata = packet.metadata
        samples = np.asarray(packet.samples)
        required = ('packet_index', 'first_frame', 'frames', 'sample_rate_hz', 'source')
        if any(key not in metadata for key in required):
            raise ValueError('PCM packet metadata lacks required continuity fields')
        index, first, frames = (metadata[key] for key in required[:3])
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
        windows = self.detector.process_block(samples)
        results = []
        for relative in windows:
            window = self._absolute_window(relative, self._base_frame)
            start = window['first_frame'] - merged_start
            stop = start + self.detector.config.window_frames
            if start < 0 or stop > len(merged):
                self.reset()
                raise RuntimeError('Detector window is unavailable in bounded PCM history')
            aoa = None
            frequency = window['system']['frequency_hz']
            if window['system']['present'] and frequency is not None:
                due = (self._last_aoa_window is None
                       or window['window_index'] - self._last_aoa_window
                       >= self.aoa_update_stride)
                if due:
                    aoa = self.estimator.estimate(
                        merged[start:stop],
                        sample_rate_hz=self.detector.config.sample_rate_hz,
                        frequency_hz=frequency,
                        first_frame=window['first_frame'],
                        window_index=window['window_index'])
                    self._last_aoa_window = window['window_index']
            else:
                self._last_aoa_window = None
            results.append({'packet_index': index, 'window': window, 'aoa': aoa,
                            'aoa_skipped': bool(window['system']['present']
                                                and frequency is not None
                                                and aoa is None)})
        retained = self.detector.config.window_frames - 1
        self._history = np.array(merged[-retained:], dtype=np.int32, copy=True)
        self._next_frame = first + frames
        self._next_packet_index = index + 1
        return results
