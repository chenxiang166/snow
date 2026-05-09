"""实时 DSP 引擎 — STFT + 重叠相加 (OLA) + WDRC 压缩"""

import math

import numpy as np
from numpy.fft import irfft, rfft

from . import constants as C

try:
    from numba import jit
    _HAS_NUMBA = True
except ImportError:
    def jit(*a, **kw):
        def d(f):
            return f
        return d
    _HAS_NUMBA = False


@jit(nopython=True)
def _wdrc_gain(idb, tg, cr, knee):
    """Return linear WDRC gain for an estimated input level."""
    if tg < 0.5:
        return 1.0
    cr = max(1.0, cr)
    if idb <= knee:
        eg = tg
    else:
        x = idb - knee
        eg = tg - (x - x / cr)
    eg = max(-20.0, min(eg, C.MAX_GAIN_DB))
    return 10.0 ** (eg / 20.0)


@jit(nopython=True)
def _compute_band_gains(spec, bins, gains, crs, knee, fft_n, win_energy,
                        full_scale_db_spl, power_floor):
    """Estimate each band level from a normalized FFT and compute WDRC gains."""
    nb = len(spec)
    nyquist_bin = fft_n // 2
    out = np.ones(len(bins), dtype=np.float32)

    for bi in range(len(bins)):
        s = max(0, bins[bi, 0])
        e = min(nb, bins[bi, 1])
        if e <= s:
            continue

        one_sided_sum = 0.0
        for k in range(s, e):
            re = spec[k].real
            im = spec[k].imag
            mag2 = re * re + im * im
            if k != 0 and k != nyquist_bin:
                mag2 *= 2.0
            one_sided_sum += mag2

        # Parseval correction for numpy's unnormalized FFT, then undo the
        # analysis-window energy to estimate the original band RMS^2.
        power = one_sided_sum / (fft_n * max(win_energy, power_floor))
        power = max(power, power_floor)
        idb = full_scale_db_spl + 10.0 * np.log10(power)
        out[bi] = _wdrc_gain(idb, gains[bi], crs[bi], knee)

    return out


@jit(nopython=True)
def _fill_rectangular_bin_gains(spec_len, bins, band_gains):
    """Compatibility helper: assign one gain per band without interpolation."""
    bin_gains = np.ones(spec_len, dtype=np.float32)
    for bi in range(len(bins)):
        s = max(0, bins[bi, 0])
        e = min(spec_len, bins[bi, 1])
        if e <= s:
            continue
        bin_gains[s:e] = band_gains[bi]
    return bin_gains


@jit(nopython=True)
def _soft_limit(samples, threshold, ceiling):
    """Fast soft limiter for float32 PCM."""
    out = samples.copy()
    knee = max(1e-6, ceiling - threshold)
    for i in range(len(out)):
        x = out[i]
        ax = abs(x)
        if ax > threshold:
            y = threshold + math.tanh((ax - threshold) / knee) * knee
            if x < 0:
                y = -y
            out[i] = y
    return out


def _apply_band_gains(spec, bins, gains, crs, knee):
    """为每个 bin 分配对应频带增益，乘一次。

    保留该函数用于测试和兼容；实时引擎会进一步做跨频插值和时间平滑。
    """
    fft_n = (len(spec) - 1) * 2
    band_gains = _compute_band_gains(
        spec, bins, np.asarray(gains, dtype=np.float32),
        np.asarray(crs, dtype=np.float32), knee, fft_n, float(fft_n),
        C.FULL_SCALE_DB_SPL, C.LEVEL_POWER_FLOOR,
    )
    bin_gains = _fill_rectangular_bin_gains(len(spec), bins, band_gains)
    return spec * bin_gains


class DSPEngine:
    def __init__(self, sr=48000, fsize=960, fft_n=2048, *,
                 sample_rate=None, frame_size=None, fft_size=None):
        # Keyword aliases keep older tests/callers working.
        if sample_rate is not None:
            sr = sample_rate
        if frame_size is not None:
            fsize = frame_size
        if fft_size is not None:
            fft_n = fft_size

        if fsize <= 0 or fsize % 2:
            raise ValueError("frame size must be a positive even number")
        if fft_n < fsize:
            raise ValueError("FFT size must be >= frame size")

        self.sr = int(sr)
        self.fsize = int(fsize)
        self.frame_size = self.fsize
        self.fft_n = int(fft_n)
        self.fft_size = self.fft_n
        self.hop = self.fsize // 2
        self.hop_size = self.hop

        # Sqrt-Hann/sine analysis+synthesis windows satisfy COLA with 50% OLA.
        n = np.arange(self.fsize, dtype=np.float32)
        self._win = np.sin(np.pi * (n + 0.5) / self.fsize).astype(np.float32)
        self._win_energy = float(np.sum(self._win ** 2))

        self._bins = np.array(self._calc_bands(self.sr, self.fft_n), dtype=np.int64)
        self._fft_freqs = np.fft.rfftfreq(self.fft_n, 1.0 / self.sr).astype(np.float32)
        self._gains = np.zeros(len(C.FREQS), dtype=np.float32)
        self._crs = np.ones(len(C.FREQS), dtype=np.float32)
        self._knee = float(C.KNEE_DB)

        self._ibuf = np.zeros(self.fsize, np.float32)
        self._ola = np.zeros(self.fsize, np.float32)
        self._band_gain_db_state = np.zeros(len(C.FREQS), dtype=np.float32)
        self._gain_state_ready = False
        self._attack_coeff = self._time_coeff(C.WDRC_ATTACK_MS)
        self._release_coeff = self._time_coeff(C.WDRC_RELEASE_MS)
        self._channel_engines = None

    def _time_coeff(self, ms):
        seconds = max(float(ms) / 1000.0, 1e-6)
        return float(math.exp(-self.hop / (self.sr * seconds)))

    @staticmethod
    def _calc_bands(sr, fft_n):
        """根据采样率动态计算无缝的测听频带 bin 范围。"""
        freqs = np.array(C.FREQS, dtype=np.float64)
        nyquist = sr / 2.0
        bin_w = sr / fft_n

        edges = [max(20.0, freqs[0] / math.sqrt(freqs[1] / freqs[0]))]
        for i in range(len(freqs) - 1):
            edges.append(math.sqrt(freqs[i] * freqs[i + 1]))
        edges.append(min(nyquist, freqs[-1] * math.sqrt(freqs[-1] / freqs[-2])))

        bins = []
        max_bin = fft_n // 2 + 1
        prev_hi = 1
        for i in range(len(freqs)):
            lo = max(1, int(round(edges[i] / bin_w)))
            hi = min(max_bin, int(round(edges[i + 1] / bin_w)))
            lo = max(lo, prev_hi)
            hi = max(lo + 1, hi)
            bins.append((lo, hi))
            prev_hi = hi
        return bins

    def update_gains(self, gain_dbs, crs=None, *legacy_args):
        """Update target gains and compression ratios.

        New API: update_gains(gain_dbs, crs)
        Legacy stereo API: update_gains(left_gains, right_gains, left_crs, right_crs)
        """
        if legacy_args:
            crs = legacy_args[0]

        gains = np.asarray(gain_dbs, dtype=np.float32)
        if gains.shape[0] != len(C.FREQS):
            raise ValueError(f"expected {len(C.FREQS)} gain values, got {gains.shape[0]}")
        self._gains = gains

        if crs is not None:
            ratios = np.asarray(crs, dtype=np.float32)
            if ratios.shape[0] != len(C.FREQS):
                raise ValueError(f"expected {len(C.FREQS)} compression ratios, got {ratios.shape[0]}")
            self._crs = np.maximum(1.0, ratios).astype(np.float32)

        self._gain_state_ready = False
        if self._channel_engines:
            for engine in self._channel_engines:
                engine.update_gains(self._gains, self._crs)

    def process_mono(self, samples):
        samples = np.asarray(samples, dtype=np.float32)
        n = len(samples)
        if n == 0:
            return np.zeros(0, np.float32)
        if n == self.fsize:
            # Backward-compatible frame API: consume a full analysis frame but
            # emit the newest hop, matching the real-time callback cadence.
            self._process_hop(samples[:self.hop])
            return self._process_hop(samples[self.hop:])
        if n != self.hop:
            return self._process_arbitrary_length(samples)
        return self._process_hop(samples)

    def process_frame(self, indata):
        """Process mono or multi-channel frames, preserving channel count."""
        data = np.asarray(indata, dtype=np.float32)
        if data.ndim == 1:
            return self.process_mono(data)
        if data.ndim != 2:
            raise ValueError("audio frame must be 1-D or 2-D")

        channels = data.shape[1]
        self._ensure_channel_engines(channels)
        outs = [self._channel_engines[ch].process_mono(data[:, ch]) for ch in range(channels)]
        out_len = min(len(o) for o in outs) if outs else 0
        return np.column_stack([o[:out_len] for o in outs]).astype(np.float32)

    def _ensure_channel_engines(self, channels):
        if self._channel_engines is not None and len(self._channel_engines) >= channels:
            return
        engines = []
        for _ in range(channels):
            engine = DSPEngine(self.sr, self.fsize, self.fft_n)
            engine.update_gains(self._gains, self._crs)
            engines.append(engine)
        self._channel_engines = engines

    def _process_arbitrary_length(self, samples):
        chunks = []
        pos = 0
        while pos < len(samples):
            chunk = np.zeros(self.hop, dtype=np.float32)
            take = min(self.hop, len(samples) - pos)
            chunk[:take] = samples[pos:pos + take]
            chunks.append(self._process_hop(chunk)[:take])
            pos += take
        return np.concatenate(chunks).astype(np.float32)

    def _process_hop(self, samples):
        self._ibuf[:-self.hop] = self._ibuf[self.hop:]
        self._ibuf[-self.hop:] = samples

        frame = np.zeros(self.fft_n, np.float32)
        frame[:self.fsize] = self._ibuf * self._win
        spec = rfft(frame)

        band_gains = _compute_band_gains(
            spec, self._bins, self._gains, self._crs, self._knee, self.fft_n,
            self._win_energy, C.FULL_SCALE_DB_SPL, C.LEVEL_POWER_FLOOR,
        )
        band_gains = self._smooth_band_gains(band_gains)
        spec *= self._interpolate_bin_gains(band_gains)

        td = irfft(spec, n=self.fft_n).astype(np.float32)
        self._ola[:self.fsize] += td[:self.fsize] * self._win

        out = self._ola[:self.hop].copy()
        self._ola[:-self.hop] = self._ola[self.hop:]
        self._ola[-self.hop:] = 0.0

        return _soft_limit(out, 0.90, 0.98).astype(np.float32)

    def _smooth_band_gains(self, band_gains):
        target_db = (20.0 * np.log10(np.maximum(band_gains, 1e-6))).astype(np.float32)
        if not self._gain_state_ready:
            self._band_gain_db_state[:] = target_db
            self._gain_state_ready = True
        else:
            coeffs = np.where(
                target_db < self._band_gain_db_state,
                self._attack_coeff,
                self._release_coeff,
            ).astype(np.float32)
            self._band_gain_db_state = (
                coeffs * self._band_gain_db_state + (1.0 - coeffs) * target_db
            ).astype(np.float32)
        return (10.0 ** (self._band_gain_db_state / 20.0)).astype(np.float32)

    def _interpolate_bin_gains(self, band_gains):
        band_db = 20.0 * np.log10(np.maximum(band_gains, 1e-6))
        nyquist = self.sr / 2.0

        xp = [float(C.LOW_FREQ_GAIN_START_HZ)]
        fp = [0.0]
        for freq, gain_db in zip(C.FREQS, band_db):
            if freq < nyquist:
                xp.append(float(freq))
                fp.append(float(gain_db))

        if nyquist > C.HIGH_FREQ_GAIN_LIMIT_HZ:
            xp.append(float(C.HIGH_FREQ_GAIN_LIMIT_HZ))
            fp.append(float(band_db[-1]))
        if nyquist > C.HIGH_FREQ_ROLLOFF_END_HZ:
            xp.append(float(C.HIGH_FREQ_ROLLOFF_END_HZ))
            fp.append(0.0)
        if xp[-1] < nyquist:
            xp.append(float(nyquist))
            fp.append(0.0)

        # Ensure strictly increasing interpolation points.
        xp_arr = np.asarray(xp, dtype=np.float32)
        fp_arr = np.asarray(fp, dtype=np.float32)
        keep = np.concatenate(([True], np.diff(xp_arr) > 1e-3))
        xp_arr = xp_arr[keep]
        fp_arr = fp_arr[keep]

        freqs = np.maximum(self._fft_freqs, 1.0)
        gain_db = np.interp(
            np.log2(freqs), np.log2(xp_arr), fp_arr, left=0.0, right=0.0
        ).astype(np.float32)
        gain_db[self._fft_freqs < C.LOW_FREQ_GAIN_START_HZ] = 0.0
        return (10.0 ** (gain_db / 20.0)).astype(np.float32)
