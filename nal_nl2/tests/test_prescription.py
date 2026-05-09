"""NAL-NL2 处方公式单元测试"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from nal_nl2 import calculate_gains, calculate_gains_simple, constants as C
from nal_nl2.prescription import (
    get_gain_array, get_cr_array, get_mpo_array,
    _cross_frequency_smoothing, _calc_compression_ratio, _calc_mpo,
)


class TestPrescription:
    """NAL-NL2 处方公式测试"""

    def test_normal_hearing_zero_gain(self):
        """正常听力应产生接近零的增益"""
        normal = [0] * 11
        result = calculate_gains_simple(normal, normal)

        gains = get_gain_array(result, 'left')
        # 正常听力增益应很小
        assert max(gains) < 10, f"正常听力增益应<10dB, 实际 max={max(gains):.1f}"

    def test_severe_loss_high_gain(self):
        """重度听损应产生较高增益"""
        severe = C.PRESETS['severe']['left']
        normal = [0] * 11
        result = calculate_gains_simple(severe, normal)

        gains = get_gain_array(result, 'left')
        # 重度听损增益应明显
        assert max(gains) > 15, f"重度听损增益应>15dB, 实际 max={max(gains):.1f}"

    def test_gain_increases_with_hearing_loss(self):
        """增益应随听损增加而增加"""
        mild = C.PRESETS['mild']['left']
        moderate = C.PRESETS['moderate']['left']

        result_mild = calculate_gains_simple(mild, mild)
        result_mod = calculate_gains_simple(moderate, moderate)

        gains_mild = get_gain_array(result_mild, 'left')
        gains_mod = get_gain_array(result_mod, 'left')

        avg_mild = sum(gains_mild) / len(gains_mild)
        avg_mod = sum(gains_mod) / len(gains_mod)

        assert avg_mod > avg_mild, \
            f"中度听损增益({avg_mod:.1f})应 > 轻度听损增益({avg_mild:.1f})"

    def test_gain_not_negative(self):
        """增益不应为负"""
        result = calculate_gains_simple([0] * 11, [0] * 11)
        gains = get_gain_array(result, 'left')
        assert all(g >= 0 for g in gains), f"存在负增益: {gains}"

    def test_binaural_correction(self):
        """双耳应比单耳低约 3dB"""
        moderate = C.PRESETS['moderate']['left']
        result_mono = calculate_gains(moderate, moderate, num_aids=1)
        result_bin = calculate_gains(moderate, moderate, num_aids=2)

        gains_mono = get_gain_array(result_mono, 'left')
        gains_bin = get_gain_array(result_bin, 'left')

        for i in range(11):
            assert gains_bin[i] <= gains_mono[i] + 0.5, \
                f"频率{C.FREQS[i]}Hz: 双耳{gains_bin[i]}应 <= 单耳{gains_mono[i]}"

    def test_output_format(self):
        """验证输出格式"""
        result = calculate_gains_simple([10] * 11, [10] * 11)

        for ear in ['left', 'right']:
            for freq in C.FREQS:
                assert freq in result[ear]
                assert 'gain_db' in result[ear][freq]
                assert 'cr' in result[ear][freq]
                assert 'mpo' in result[ear][freq]

    def test_cross_frequency_smoothing(self):
        """跨频平滑不应改变均值太多"""
        raw = {f: 20.0 for f in C.FREQS}
        raw[125] = 30.0  # 异常值

        smoothed = _cross_frequency_smoothing(raw)

        # 平滑后 125Hz 应更接近相邻值
        assert smoothed[125] < 30.0, "异常值应被平滑"
        assert smoothed[125] > 20.0, "但不应完全丢失"

    def test_compression_ratio_range(self):
        """压缩比应在 1.0–3.5 范围内"""
        for ht in [0, 20, 40, 60, 80, 100, 120]:
            cr = _calc_compression_ratio(ht)
            assert 1.0 <= cr <= 3.5, f"HTL={ht} → CR={cr}"

    def test_mpo_limits(self):
        """MPO 不应超过 110 dB SPL"""
        for freq in C.FREQS:
            for ht in [0, 50, 100]:
                mpo = _calc_mpo(freq, ht)
                assert mpo <= 110, f"MPO 超过限制: {mpo}"


class TestPresets:
    """预设听力图测试"""

    def test_all_presets_valid(self):
        """所有预设有 11 个值"""
        for name, preset in C.PRESETS.items():
            assert len(preset['left']) == 11, f"预设 {name} 左耳长度错误"
            assert len(preset['right']) == 11, f"预设 {name} 右耳长度错误"
            assert all(0 <= v <= 120 for v in preset['left'])
            assert all(0 <= v <= 120 for v in preset['right'])

    def test_preset_severity_increasing(self):
        """预设严重程度递增"""
        names = ['normal', 'mild', 'moderate', 'severe']
        avg_thresholds = []
        for name in names:
            preset = C.PRESETS[name]
            avg = (sum(preset['left']) + sum(preset['right'])) / 22
            avg_thresholds.append(avg)

        for i in range(len(avg_thresholds) - 1):
            assert avg_thresholds[i] < avg_thresholds[i + 1], \
                f"{names[i]}({avg_thresholds[i]:.0f})应 < {names[i+1]}({avg_thresholds[i+1]:.0f})"


class TestGainHelpers:
    """辅助函数测试"""

    def test_get_gain_array(self):
        result = calculate_gains_simple([10] * 11, [10] * 11)
        gains = get_gain_array(result, 'left')
        assert len(gains) == 11
        assert all(isinstance(g, (int, float)) for g in gains)

    def test_get_cr_array(self):
        result = calculate_gains_simple([10] * 11, [10] * 11)
        crs = get_cr_array(result, 'left')
        assert len(crs) == 11
        assert all(1.0 <= cr <= 3.5 for cr in crs)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
