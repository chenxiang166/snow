"""多频带滤波器组 — 时域 FIR 滤波器替代方案

在大多数情况下使用频域 (STFT + 频带增益) 更高效。
此模块提供时域多频带滤波器作为备选, 用于需要零延迟的单频带处理。
"""

import numpy as np
from scipy import signal
from . import constants as C


def design_bandpass_filters(
    sample_rate: int = C.SAMPLE_RATE,
    num_taps: int = 256,
) -> list:
    """
    为每个标准测听频率设计一个带通 FIR 滤波器。

    返回:
        [(b, a), ...] 每个频带的滤波器系数
    """
    filters = []
    freq_edges = [0] + C.FREQS + [sample_rate // 2]

    for i in range(len(C.FREQS)):
        low = freq_edges[i]
        high = freq_edges[i + 2]  # 跳过相邻频率

        if i == 0:
            low = 0
        if i == len(C.FREQS) - 1:
            high = sample_rate // 2

        # 归一化截止频率
        nyquist = sample_rate / 2
        low_norm = max(low / nyquist, 0.01)
        high_norm = min(high / nyquist, 0.99)

        # 设计 FIR 带通滤波器
        b = signal.firwin(num_taps, [low_norm, high_norm],
                          pass_zero=False, window='hann')
        filters.append(b)

    return filters


def apply_filterbank(samples: np.ndarray, filters: list,
                    gains: list[float]) -> np.ndarray:
    """
    使用时域滤波器组处理音频。

    参数:
        samples: 输入采样 (1D array)
        filters: 各频带 FIR 滤波器系数列表
        gains:   各频带增益 (线性值)

    返回:
        处理后的采样
    """
    output = np.zeros_like(samples)
    for b, gain in zip(filters, gains):
        filtered = signal.lfilter(b, 1.0, samples)
        output += filtered * (10.0 ** (gain / 20.0))
    return output
