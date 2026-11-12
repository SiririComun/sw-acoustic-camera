"""Offline PCM bench measurements, preserving the captured sample values.

Levels are relative to digital full scale, not calibrated sound pressure. Delay
estimates from acoustic signals do not establish electronic channel alignment.
"""
import math
import numpy as np


def _samples(samples):
    values = np.asarray(samples)
    if values.dtype != np.dtype('int32') or values.ndim != 2 or values.shape[1] != 4 or len(values) < 16:
        raise ValueError('Expected at least 16 synchronous frames as int32 (N,4)')
    if np.any(values < -8388608) or np.any(values > 8388607):
        raise ValueError('Samples are not sign-extended signed24')
    return values.astype(np.float64) / 2**23


def _rate(sample_rate_hz):
    if isinstance(sample_rate_hz, bool) or not isinstance(sample_rate_hz, (int, float)) or not math.isfinite(sample_rate_hz) or sample_rate_hz <= 10000:
        raise ValueError('sample_rate_hz must be finite and greater than 10000')
    return float(sample_rate_hz)


def pcm_spectrum(samples, sample_rate_hz=48000):
    """Hann-windowed one-sided amplitude spectrum, normalized to digital FS."""
    x = _samples(samples)
    fs = _rate(sample_rate_hz)
    x -= x.mean(axis=0)
    window = np.hanning(len(x))
    amplitude = 2*np.abs(np.fft.rfft(x*window[:, None], axis=0))/window.sum()
    amplitude[0] *= 0.5
    if len(x) % 2 == 0:
        amplitude[-1] *= 0.5
    return np.fft.rfftfreq(len(x), 1/fs), amplitude


def analyze_pcm(samples, sample_rate_hz=48000, *, tone_hz=None):
    """DC/AC levels and the project's Hann tone-versus-neighbor-floor check.

    The test tone is expected to dominate 0.5–5 kHz. A pass requires its peak
    within 2% of the nominal tone and at least 10 dB above the median neighbor
    bin power, excluding +/-100 Hz about the peak. This is a bench metric, not
    an instrument SNR/THD specification. Silence produces no tone pass.
    """
    x = _samples(samples)
    fs = _rate(sample_rate_hz)
    if tone_hz is not None and (isinstance(tone_hz, bool) or not isinstance(tone_hz, (int, float)) or not math.isfinite(tone_hz) or not 500 <= tone_hz <= 5000):
        raise ValueError('tone_hz must be finite within 500..5000 Hz')
    frequencies, amplitude = pcm_spectrum(samples, fs)
    dc = x.mean(axis=0)
    ac = x-dc
    channels = []
    band = (frequencies >= 500) & (frequencies <= 5000)
    if not np.any(band):
        raise ValueError('Capture too short for the bench frequency band')
    indices = np.flatnonzero(band)
    for c in range(4):
        peak = int(indices[np.argmax(amplitude[band, c])])
        active = bool(np.any(ac[:, c]))
        peak_hz = float(frequencies[peak]) if active else None
        neighbors = band & (np.abs(frequencies-frequencies[peak]) > 100)
        power = amplitude[:, c]**2
        floor = float(np.median(power[neighbors])) if np.any(neighbors) else 0.0
        over_floor = None
        if active and power[peak] > 0:
            tiny = np.finfo(float).tiny
            over_floor = 10*(math.log10(max(float(power[peak]), tiny))-math.log10(max(floor, tiny)))
        ac_rms = float(np.sqrt(np.mean(ac[:, c]**2)))
        checks = None
        if tone_hz is not None:
            checks = dict(nominal_hz=float(tone_hz), frequency_within_2_percent=active and abs(peak_hz-tone_hz) <= 0.02*tone_hz,
                          peak_at_least_10_db_over_floor=over_floor is not None and over_floor >= 10)
            checks['pass'] = checks['frequency_within_2_percent'] and checks['peak_at_least_10_db_over_floor']
        channels.append(dict(channel=c, dc_full_scale=float(dc[c]), ac_rms_full_scale=ac_rms,
                             ac_rms_dbfs=20*math.log10(ac_rms) if ac_rms else None,
                             peak_abs_full_scale=float(np.max(np.abs(x[:, c]))),
                             endpoint_samples=int(np.sum((samples[:, c] == -8388608) | (samples[:, c] == 8388607))),
                             band_peak_hz=peak_hz, peak_over_neighbor_floor_db=over_floor, tone_check=checks))
    return dict(n_frames=len(x), nominal_sample_rate_hz=fs, nominal_duration_seconds=len(x)/fs,
                fft_bin_spacing_hz=fs/len(x), method='mean removal; Hann; median bin power 500..5000 Hz excluding +/-100 Hz about peak',
                endpoint_samples_are_not_hardware_clip_count=True, channels=channels)


def estimate_pair_delays(samples, sample_rate_hz=48000, *, max_lag_samples=16):
    """Six integer-lag correlations in 1–3 kHz; positive lag means j follows i.

    Use broadband sound, record geometry and repeat. Pure tones can give
    ambiguous delays. A lag at the search boundary needs a wider search/review.
    """
    x = _samples(samples)
    fs = _rate(sample_rate_hz)
    if type(max_lag_samples) is not int or not 0 <= max_lag_samples < len(x)//2:
        raise ValueError('max_lag_samples must be an integer below N/2')
    x -= x.mean(axis=0)
    frequencies = np.fft.rfftfreq(len(x), 1/fs)
    spectrum = np.fft.rfft(x, axis=0)
    spectrum[(frequencies < 1000) | (frequencies > 3000)] = 0
    filtered = np.fft.irfft(spectrum, n=len(x), axis=0)
    pairs = []
    for i in range(4):
        for j in range(i+1, 4):
            scores = []
            lags = range(-max_lag_samples, max_lag_samples+1)
            for lag in lags:
                if lag >= 0:
                    a, b = filtered[:len(x)-lag, i], filtered[lag:, j]
                else:
                    a, b = filtered[-lag:, i], filtered[:len(x)+lag, j]
                norm = np.sqrt(np.dot(a, a)*np.dot(b, b))
                scores.append(float(np.dot(a, b)/norm) if norm else 0.0)
            active = bool(np.any(filtered[:, i]) and np.any(filtered[:, j]))
            best = int(np.argmax(scores))
            lag = list(lags)[best] if active else None
            pairs.append(dict(channels=[i, j], lag_samples=lag, nominal_delay_seconds=lag/fs if active else None,
                              correlation=scores[best] if active else None,
                              at_search_limit=active and max_lag_samples > 0 and abs(lag) == max_lag_samples))
    return dict(method='1..3 kHz FFT band selection; normalized correlation; integer lag',
                positive_lag='second channel follows first', max_lag_samples=max_lag_samples, pairs=pairs)
