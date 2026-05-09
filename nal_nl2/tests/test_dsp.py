"""DSP 引擎单元测试 — STFT + WDRC 压缩"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import numpy as np
import pytest

from nal_nl2.dsp_engine import DSPEngine, _wdrc_gain, _apply_band_gains
from nal_nl2 import constants as C


class TestDSPEngine:
    """DSP 引擎测试"""

    @pytest.fixture
    def dsp(self):
        """创建 DSP 引擎实例"""
        engine = DSPEngine(sample_rate=48000, frame_size=960, fft_size=2048)
        engine.update_gains(
            [10.0] * 11,  # 左耳增益 10dB
            [10.0] * 11,  # 右耳增益 10dB
            [1.5] * 11,   # 压缩比 1.5:1
            [1.5] * 11,
        )
        return engine

    def test_process_silence(self, dsp):
        """静音输入应产生静音输出"""
        silence = np.zeros(dsp.frame_size, dtype=np.float32)
        output = dsp.process_mono(silence)
        assert np.all(output == 0), "静音输入应产生静音输出"

    def test_process_sine(self, dsp):
        """正弦波处理后不应损坏"""
        t = np.arange(dsp.frame_size) / C.SAMPLE_RATE
        sine = np.sin(2 * np.pi * 1000 * t).astype(np.float32)

        output = dsp.process_mono(sine)
        assert output.shape[0] == dsp.hop_size
        assert not np.all(output == 0), "输出不应全为零"

    def test_gain_applied(self, dsp):
        """增益应被应用"""
        t = np.arange(dsp.frame_size) / C.SAMPLE_RATE
        sine = np.sin(2 * np.pi * 1000 * t).astype(np.float32)

        # 无增益
        dsp.update_gains([0.0] * 11, [0.0] * 11)
        output_no_gain = dsp.process_mono(sine.copy())

        # 有增益
        dsp.update_gains([20.0] * 11, [20.0] * 11)
        output_with_gain = dsp.process_mono(sine.copy())

        rms_no_gain = np.sqrt(np.mean(output_no_gain ** 2))
        rms_with_gain = np.sqrt(np.mean(output_with_gain ** 2))

        assert rms_with_gain > rms_no_gain * 1.5, \
            f"增益应显著增加 RMS (无:{rms_no_gain:.4f}, 有:{rms_with_gain:.4f})"

    def test_process_stereo(self, dsp):
        """立体声处理不应崩溃"""
        indata = np.random.randn(dsp.frame_size, 2).astype(np.float32) * 0.1
        output = dsp.process_frame(indata)
        assert output.shape[0] == dsp.hop_size
        assert output.shape[1] == 2

    def test_processing_latency(self, dsp):
        """处理延迟应远低于帧预算"""
        t = np.arange(dsp.frame_size) / C.SAMPLE_RATE
        sine = np.sin(2 * np.pi * 1000 * t).astype(np.float32)

        # 预热
        for _ in range(10):
            dsp.process_mono(sine)

        # 测量
        times = []
        for _ in range(50):
            start = time.perf_counter()
            dsp.process_mono(sine)
            elapsed = (time.perf_counter() - start) * 1000  # ms
            times.append(elapsed)

        avg_time = np.mean(times)
        max_time = np.max(times)

        # 应在 5ms 以内 (远低于 20ms 帧预算)
        assert avg_time < 5.0, f"平均处理时间 {avg_time:.2f}ms 超标"
        assert max_time < 10.0, f"最大处理时间 {max_time:.2f}ms 超标"

    def test_max_output_limit(self, dsp):
        """极大输入应被压缩/限制"""
        loud = np.ones(dsp.frame_size, dtype=np.float32) * 0.9

        dsp.update_gains([30.0] * 11, [30.0] * 11, [3.0] * 11, [3.0] * 11)
        output = dsp.process_mono(loud)

        # 输出不应超过 1.0 (满量程)
        assert np.max(np.abs(output)) <= 1.0 + 0.1, \
            f"输出超过满量程: {np.max(np.abs(output)):.3f}"

    def test_frequency_specific_gain(self, dsp):
        """频率特定增益: 高频增益应放大高频"""
        t = np.arange(dsp.frame_size) / C.SAMPLE_RATE
        low_freq = np.sin(2 * np.pi * 300 * t).astype(np.float32) * 0.5
        high_freq = np.sin(2 * np.pi * 4000 * t).astype(np.float32) * 0.5

        # 仅高频增益
        gains = [0.0] * 11
        gains[8] = 20.0  # 4000 Hz = +20dB
        gains[9] = 20.0  # 6000 Hz = +20dB
        dsp.update_gains(gains, gains)

        out_low = dsp.process_mono(low_freq)
        out_high = dsp.process_mono(high_freq)

        rms_low = np.sqrt(np.mean(out_low ** 2))
        rms_high = np.sqrt(np.mean(out_high ** 2))

        assert rms_high > rms_low * 2, \
            f"高频增益应放高高频 ({rms_low:.4f} vs {rms_high:.4f})"


class TestWDRCGain:
    """WDRC 压缩函数测试"""

    def test_linear_region(self):
        """低于拐点时不应压缩"""
        gain = _wdrc_gain(40.0, 10.0, 2.0, 50.0)
        # 输入 40dB + 增益 10dB = 输出应接近 10dB 增益 (线性)
        assert gain > 1.0

    def test_compression_region(self):
        """高于拐点时应压缩"""
        gain_linear = _wdrc_gain(40.0, 10.0, 2.0, 50.0)
        gain_compressed = _wdrc_gain(70.0, 10.0, 2.0, 50.0)

        # 压缩后的增益应更小
        assert gain_compressed < gain_linear, \
            f"压缩增益({gain_compressed:.3f})应 < 线性增益({gain_linear:.3f})"

    def test_higher_cr_more_compression(self):
        """更高压缩比 → 更低增益"""
        gain_low_cr = _wdrc_gain(70.0, 10.0, 1.5, 50.0)
        gain_high_cr = _wdrc_gain(70.0, 10.0, 3.0, 50.0)

        assert gain_high_cr < gain_low_cr, \
            f"高压缩比增益({gain_high_cr:.3f})应 < 低压缩比增益({gain_low_cr:.3f})"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
