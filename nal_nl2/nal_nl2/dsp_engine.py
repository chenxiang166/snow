"""实时 DSP 引擎 — STFT + 重叠相加 (OLA) + WDRC 压缩"""

import numpy as np
from numpy.fft import rfft, irfft
from . import constants as C

try:
    from numba import jit
    _HAS_NUMBA = True
except ImportError:
    def jit(*a,**kw):
        def d(f): return f
        return d
    _HAS_NUMBA = False

@jit(nopython=True)
def _wdrc_gain(idb, tg, cr, knee):
    # 目标增益接近 0 → 不做任何处理
    if tg < 0.5:
        return 1.0
    if idb <= knee:
        eg = tg
    else:
        x = idb - knee
        eg = tg - (x - x / cr)
    eg = max(-20.0, min(eg, C.MAX_GAIN_DB))
    return 10.0 ** (eg / 20.0)

@jit(nopython=True)
def _apply_band_gains(spec, bins, gains, crs, knee):
    """为每个 bin 分配对应频带增益，乘一次"""
    nb = len(spec)
    # 为每个 bin 确定增益: 找到覆盖该 bin 的频带
    bin_gains = np.ones(nb, dtype=np.float32)
    for bi in range(len(bins)):
        s, e = max(0, bins[bi, 0]), min(nb, bins[bi, 1])
        if e <= s:
            continue
        m = np.abs(spec[s:e])
        p = np.sum(m ** 2) / (e - s)
        if p < 1e-12:
            continue
        idb = 94.0 + 10 * np.log10(p + 1e-12)
        lg = _wdrc_gain(idb, gains[bi], crs[bi], knee)
        bin_gains[s:e] = lg  # 覆盖前面的值, 边界 bin 以最后一个频带为准
    return spec * bin_gains

class DSPEngine:
    def __init__(self, sr=48000, fsize=960, fft_n=2048):
        self.sr = sr; self.fsize = fsize; self.fft_n = fft_n
        self.hop = fsize // 2
        self._win = np.sin(np.pi * np.arange(fsize) / fsize).astype(np.float32)  # Sine窗, 50%OLA完美重建
        # 根据实际采样率动态计算频带 bin 范围
        self._bins = np.array(self._calc_bands(sr, fft_n), dtype=np.int64)
        self._gains = np.zeros(11, dtype=np.float32)
        self._crs = np.ones(11, dtype=np.float32)
        self._knee = C.KNEE_DB
        self._ibuf = np.zeros(fsize, np.float32)
        self._ola = np.zeros(fft_n + fsize//2, np.float32)

    @staticmethod
    def _calc_bands(sr, fft_n):
        """根据采样率动态计算频带 bin 范围"""
        bins = []
        freqs = C.FREQS  # [125, 250, ..., 8000]
        bin_w = sr / fft_n
        for i, f in enumerate(freqs):
            if i == 0:
                lo = max(1, int((freqs[0] * 0.5) / bin_w))
            else:
                lo = bins[-1][1] + 1  # 紧接上一频带, 不重叠
            if i == len(freqs) - 1:
                hi = min(fft_n // 2, int((f * 1.3) / bin_w))
            else:
                hi = int(((f + freqs[i+1]) / 2) / bin_w)
            bins.append((lo, max(lo + 1, hi)))
        return bins

    def update_gains(self, gain_dbs, crs=None):
        self._gains = np.array(gain_dbs, dtype=np.float32)
        if crs is not None:
            self._crs = np.array(crs, dtype=np.float32)

    def process_mono(self, samples):
        n = len(samples); out = np.zeros(n, np.float32)
        hop = self.hop; fsize = self.fsize; fft_n = self.fft_n
        # block-based OLA: shift buffer, add new, process, OLA output
        self._ibuf[:fsize-hop] = self._ibuf[hop:]
        self._ibuf[fsize-hop:] = samples
        
        w = np.zeros(fft_n, np.float32)
        w[:fsize] = self._ibuf * self._win
        spec = rfft(w)
        spec = _apply_band_gains(spec, self._bins, self._gains, self._crs, self._knee)
        td = irfft(spec, n=fft_n).astype(np.float32)
        self._ola[:fft_n] += td
        out[:] = self._ola[:hop]
        self._ola[:fft_n] = np.roll(self._ola[:fft_n+hop], -hop)[:fft_n]
        self._ola[fft_n:] = 0
        # 自适应限制: 超出 ±0.95 时软压缩
        over = np.abs(out) - 0.9
        mask = over > 0
        out[mask] = np.sign(out[mask]) * (0.9 + np.tanh(over[mask] * 2) * 0.09)
        return out