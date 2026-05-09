"""NAL-NL2 处方公式 — 根据听力图计算目标增益与压缩比

参考文献:
  Keidser, G., Dillon, H., Flax, M., Ching, T., & Brewer, S. (2011).
  The NAL-NL2 Prescription Procedure. Audiology Research, 1(1).

核心流程:
  1. 逐频率计算基础增益: G(f) = a(f) * HTL(f) + b(f)
  2. 应用修正: 双耳/性别/经验/年龄
  3. 跨频平滑
  4. 计算压缩比
  5. 计算 MPO 限制
"""

import math
from . import constants as C


# ── 辅助函数 ──

def _gain_a(freq: int) -> float:
    """频率相关增益系数 a(f)"""
    if freq not in C.FREQS:
        # 对于不在标准频率表中的频率，使用最近频率的系数
        return _gain_a(_nearest_freq(freq))
    idx = C.FREQS.index(freq)
    return C.A_COEFFS[idx]


def _gain_b(freq: int) -> float:
    """频率相关增益偏移 b(f)"""
    if freq not in C.FREQS:
        return _gain_b(_nearest_freq(freq))
    idx = C.FREQS.index(freq)
    return C.B_COEFFS[idx]


def _nearest_freq(freq: int) -> int:
    """找到最近的标准测听频率"""
    return min(C.FREQS, key=lambda f: abs(f - freq))


def _estimate_ucl(freq: int, htl_db: float) -> float:
    """
    估算不适阈 (UCL, Uncomfortable Loudness Level)
    简化模型: UCL = 90 + HTL * 0.3, 上限 120 dB SPL
    """
    return min(120.0, 90.0 + htl_db * 0.3)


# ── 主计算公式 ──

def calculate_gains(
    left_htl: list[float],
    right_htl: list[float],
    num_aids: int = 2,
    gender: str = 'M',
    experience: str = 'new',
    age: str = 'adult',
) -> dict:
    """
    计算 NAL-NL2 增益、压缩比和 MPO。

    参数:
        left_htl:   左耳听阈列表 (dB HL), 11 个值对应 FREQS
        right_htl:  右耳听阈列表 (dB HL), 11 个值对应 FREQS
        num_aids:   助听器数量 (1=单耳, 2=双耳)
        gender:     性别 ('M' 或 'F')
        experience: 使用经验 ('new' 或 'experienced')
        age:        年龄组 ('adult' 或 'child')

    返回:
        {
            'left':  {freq: {'gain_db': float, 'cr': float, 'mpo': float}, ...},
            'right': {freq: {'gain_db': float, 'cr': float, 'mpo': float}, ...},
        }
    """
    # 校验输入长度
    assert len(left_htl) == 11, f"左耳听阈应为11个值, 实际 {len(left_htl)}"
    assert len(right_htl) == 11, f"右耳听阈应为11个值, 实际 {len(right_htl)}"

    # 修正值
    binaural_correction = C.BINAURAL_CORRECTION if num_aids == 2 else 0.0
    gender_correction = C.GENDER_CORRECTION.get(gender, 0.0)
    experience_correction = C.EXPERIENCE_CORRECTION.get(experience, 0.0)
    age_correction = C.AGE_CORRECTION.get(age, 0.0)

    def _process_ear(htl_list: list[float]) -> dict:
        # Step 1: 各频率基础增益
        raw_gains = {}
        for i, freq in enumerate(C.FREQS):
            ht = htl_list[i]
            a = C.A_COEFFS[i]
            b = C.B_COEFFS[i]

            # 核心公式: G(f) = a(f) * HTL(f) + b(f) + corrections
            gain = a * ht + b
            gain += binaural_correction
            gain += gender_correction
            gain += experience_correction
            gain += age_correction

            # 钳制
            gain = max(C.MIN_GAIN_DB, min(C.MAX_GAIN_DB, gain))
            raw_gains[freq] = gain

        # Step 2: 跨频平滑 (3点移动平均)
        smoothed = _cross_frequency_smoothing(raw_gains)

        # Step 3: 计算压缩比 & MPO
        result = {}
        for i, freq in enumerate(C.FREQS):
            ht = htl_list[i]
            gain_db = round(smoothed[freq], 1)
            cr = _calc_compression_ratio(ht)
            mpo = _calc_mpo(freq, ht)

            result[freq] = {
                'gain_db': gain_db,
                'cr': round(cr, 2),
                'mpo': round(mpo, 1),
            }
        return result

    return {
        'left': _process_ear(left_htl),
        'right': _process_ear(right_htl),
    }


def _cross_frequency_smoothing(gains: dict) -> dict:
    """
    跨频平滑: 对相邻频率做 3 点平均

    G_smooth(f_i) = (G(f_{i-1}) + G(f_i) + G(f_{i+1})) / 3
    """
    smoothed = {}
    for i, freq in enumerate(C.FREQS):
        if i == 0:
            # 左边界: (G0 * 2 + G1) / 3
            smoothed[freq] = (gains[freq] * 2 + gains[C.FREQS[1]]) / 3
        elif i == len(C.FREQS) - 1:
            # 右边界
            smoothed[freq] = (gains[C.FREQS[-2]] * 2 + gains[freq]) / 3
        else:
            smoothed[freq] = (
                gains[C.FREQS[i - 1]] + gains[freq] + gains[C.FREQS[i + 1]]
            ) / 3
    return smoothed


def _calc_compression_ratio(htl_db: float) -> float:
    """
    根据听阈计算压缩比 (Compression Ratio)

    CR(f) = max(1.0, HTL(f) / CR_K + 1.0)
    范围: 1.0 ~ 3.5
    """
    cr = htl_db / C.CR_K + 1.0
    return max(1.0, min(3.5, cr))


def _calc_mpo(freq: int, htl_db: float) -> float:
    """
    计算最大输出限制 (MPO, Maximum Power Output)

    MPO(f) = min(UCL(f), DEFAULT_MPO)
    """
    ucl = _estimate_ucl(freq, htl_db)
    return min(ucl, C.DEFAULT_MPO)


# ── 便捷函数 ──

def calculate_gains_simple(
    left_htl: list[float],
    right_htl: list[float],
) -> dict:
    """使用默认配置 (双耳/男/新用户/成人) 计算增益"""
    return calculate_gains(left_htl, right_htl,
                           num_aids=2, gender='M',
                           experience='new', age='adult')


def get_gain_array(result: dict, ear: str = 'left') -> list[float]:
    """从计算结果中提取增益数组 (用于 DSP)"""
    return [result[ear][freq]['gain_db'] for freq in C.FREQS]


def get_cr_array(result: dict, ear: str = 'left') -> list[float]:
    """从计算结果中提取压缩比数组 (用于 DSP)"""
    return [result[ear][freq]['cr'] for freq in C.FREQS]


def get_mpo_array(result: dict, ear: str = 'left') -> list[float]:
    """从计算结果中提取 MPO 数组"""
    return [result[ear][freq]['mpo'] for freq in C.FREQS]
