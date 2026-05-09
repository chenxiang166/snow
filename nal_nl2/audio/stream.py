"""音频流管理 — 全双工音频流封装 (Loopback → DSP → Output)"""

import sys
import os
import json
import time
import atexit
import signal
import threading
import logging

import numpy as np
import sounddevice as sd

from . import loopback as lb
from . import volume_control as vc
from nal_nl2.dsp_engine import DSPEngine
from nal_nl2 import constants as C

logger = logging.getLogger(__name__)

# 状态文件路径
STATE_FILE = os.path.join(os.path.expanduser('~'), '.pc_audio_comp_state.json')


# ── 状态文件管理 (Layer 3: 启动时脏状态恢复) ──

def _write_state(active: bool, original_mute: bool = None,
                original_volume: float = None):
    """写入状态文件"""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({
                'active': active,
                'original_mute': original_mute,
                'original_volume': original_volume,
                'pid': os.getpid(),
                'timestamp': time.time(),
            }, f)
    except Exception:
        pass


def _read_state() -> dict | None:
    """读取状态文件"""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def check_and_recover():
    """
    启动时检查并恢复脏状态 (Layer 3)。

    如果上次运行异常退出 (active=True 但旧进程已死)，
    自动恢复系统静音和音量。
    """
    state = _read_state()
    if not state or not state.get('active'):
        return

    pid = state.get('pid', 0)
    try:
        os.kill(pid, 0)  # 检查进程是否存在
        logger.info(f"旧进程 (PID={pid}) 仍在运行，不恢复")
        return
    except OSError:
        # 进程已死 — 脏退出
        logger.warning(f"检测到异常退出 (PID={pid})，恢复音频设置...")
        try:
            vc.restore_default_device(
                state.get('original_mute', False),
                state.get('original_volume', 1.0),
            )
            logger.info("音频设置已恢复")
        except Exception as e:
            logger.error(f"恢复失败: {e}")
        finally:
            try:
                os.remove(STATE_FILE)
            except Exception:
                pass


# ── 音频流管理器 ──

class AudioStream:
    """全双工音频流: Loopback Input → DSP → Physical Output"""

    def __init__(self, sample_rate: int = C.SAMPLE_RATE,
                 frame_size: int = C.FRAME_SIZE,
                 channels: int = 2):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.channels = channels
        self.hop_size = frame_size // 2

        # 设备
        self._input_id: int | None = None
        self._output_id: int | None = None
        self._input_mode: str = 'loopback'  # 'loopback' or 'stereo_mix'

        # 流
        self._stream: sd.Stream | None = None

        # DSP
        self._dsp_left = DSPEngine(sample_rate, frame_size, C.FFT_SIZE)
        self._dsp_right = DSPEngine(sample_rate, frame_size, C.FFT_SIZE)

        # 音频状态
        self._active = False
        self._original_mute = None
        self._original_volume = None
        self._current_gain_result = None
        self.uniform_test = False  # 均匀增益测试模式
        self.master_volume = 1.0

        # 音量同步 (VB-Cable 模式下同步系统音量到物理输出)
        self._vol_sync_timer: threading.Timer | None = None
        self._vol_sync_interval = 0.1

        # 统计
        self._frame_count = 0
        self._stats_lock = threading.Lock()
        self._peak_input_db = -120.0
        self._processing_time_ms = 0.0

    # ── 公共接口 ──

    def set_audiogram(self, left_htl: list[float], right_htl: list[float],
                      config: dict = None):
        """
        设置听力图并计算 NAL-NL2 增益。

        参数:
            left_htl:  左耳听阈 (11 个值, dB HL)
            right_htl: 右耳听阈 (11 个值, dB HL)
            config:    可选配置 dict
        """
        from nal_nl2.prescription import calculate_gains

        if config is None:
            config = {'num_aids': 2, 'gender': 'M',
                      'experience': 'new', 'age': 'adult'}

        self._current_gain_result = calculate_gains(
            left_htl, right_htl,
            num_aids=config.get('num_aids', 2),
            gender=config.get('gender', 'M'),
            experience=config.get('experience', 'new'),
            age=config.get('age', 'adult'),
        )

        # 提取增益和压缩比数组
        from nal_nl2.prescription import get_gain_array, get_cr_array
        left_gains = get_gain_array(self._current_gain_result, 'left')
        right_gains = get_gain_array(self._current_gain_result, 'right')
        left_crs = get_cr_array(self._current_gain_result, 'left')
        right_crs = get_cr_array(self._current_gain_result, 'right')

        # 更新 DSP
        self._dsp_left.update_gains(left_gains, left_crs)
        self._dsp_right.update_gains(right_gains, right_crs)

    def start(self) -> bool:
        """
        启动音频流。

        返回:
            成功返回 True
        """
        if self._active:
            logger.warning("音频流已在运行")
            return True

        try:
            # 自动查找输入设备, 保留用户已选的输出设备
            self._input_id, auto_output_id, self._input_mode = lb.get_device_pair()
            if self._output_id is None:
                self._output_id = auto_output_id
            # else: 保持用户在 GUI 中选择的输出设备
            lb.print_device_info(self._input_id, self._output_id, self._input_mode)

            # VB-Cable: 完美方案 — 系统音频走虚拟设备, 输出到物理扬声器
            if self._input_mode == 'vb_cable':
                logger.info("✅ 使用 VB-Cable 虚拟声卡")
            elif self._input_mode == 'stereo_mix':
                logger.info("使用 立体声混音 作为输入源 (Loopback 不可用)")
            
            lb.print_device_info(self._input_id, self._output_id, self._input_mode)
        except RuntimeError as e:
            logger.error(f"设备查找失败: {e}")
            return False

        try:
            # 保存原始状态
            self._original_mute, self._original_volume = vc.get_default_device_state()

            # VB-Cable: 系统音频自动走虚拟设备 (已设为默认)
            logger.info("VB-Cable 模式" if self._input_mode == 'vb_cable' else "保留系统原声")

            # 写入状态文件 (Layer 3)
            _write_state(True, self._original_mute, self._original_volume)

            # 确定输入声道数和采样率
            input_device = sd.query_devices()[self._input_id]
            output_device = sd.query_devices()[self._output_id]
            in_channels = min(input_device['max_input_channels'], 2)
            # 使用输出设备的采样率，避免重采样
            actual_sr = int(output_device['default_samplerate'])
            if actual_sr < 8000 or actual_sr > 192000:
                actual_sr = self.sample_rate

            # 同步 DSP 采样率
            if actual_sr != self._dsp_left.sr:
                self._dsp_left = DSPEngine(actual_sr, self.frame_size, C.FFT_SIZE)
                self._dsp_right = DSPEngine(actual_sr, self.frame_size, C.FFT_SIZE)
                # 重新应用增益
                if self._current_gain_result:
                    from nal_nl2.prescription import get_gain_array, get_cr_array
                    lg = get_gain_array(self._current_gain_result, 'left')
                    rg = get_gain_array(self._current_gain_result, 'right')
                    lc = get_cr_array(self._current_gain_result, 'left')
                    rc = get_cr_array(self._current_gain_result, 'right')
                    self._dsp_left.update_gains(lg, lc)
                    self._dsp_right.update_gains(rg, rc)

            # 打开音频流 (blocksize = hop_size, DSP 内部做 OLA)
            self._stream = sd.Stream(
                samplerate=actual_sr,
                blocksize=self.hop_size,
                device=(self._input_id, self._output_id),
                channels=(in_channels, self.channels),
                dtype='float32',
                callback=self._audio_callback,
            )
            self._stream.start()
            self._active = True
            if self._input_mode == 'vb_cable':
                self._start_volume_sync()
            else:
                self.master_volume = 1.0
            logger.info("音频流已启动")
            return True

        except Exception as e:
            logger.error(f"启动失败: {e}")
            self._cleanup()
            return False

    def stop(self):
        """停止音频流并恢复系统设置"""
        if not self._active:
            return

        self._active = False
        self._stop_volume_sync()

        # 停止流
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        self._cleanup()
        logger.info("音频流已停止")

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def gain_result(self) -> dict | None:
        return self._current_gain_result

    @property
    def stats(self) -> dict:
        """返回实时统计信息"""
        with self._stats_lock:
            return {
                'frame_count': self._frame_count,
                'peak_input_db': self._peak_input_db,
                'processing_time_ms': self._processing_time_ms,
                'latency_ms': (sum(self._stream.latency) / 2 * 1000) if self._stream and hasattr(self._stream, 'latency') else 0,
            }

    # ── 内部实现 ──

    def _audio_callback(self, indata: np.ndarray, outdata: np.ndarray,
                        frames: int, time_info, status):
        if status:
            logger.warning(f"音频回调状态: {status}")

        if not self._active:
            outdata.fill(0)
            return

        try:
            rms = np.sqrt(np.mean(indata ** 2))
            if rms < 1e-7:
                outdata.fill(0)
                return

            self._frame_count += 1
            with self._stats_lock:
                peak_db = 20 * np.log10(max(rms, 1e-10))
                self._peak_input_db = max(self._peak_input_db, peak_db)

            # 真直通: 若所有增益 < 0.5dB, 原样输出
            if self._dsp_left._gains.max() < 0.5 and self._dsp_right._gains.max() < 0.5:
                outdata.fill(0)
                if indata.shape[1] == 1 and outdata.shape[1] >= 2:
                    outdata[:] = indata[:, :1]
                else:
                    ch = min(indata.shape[1], outdata.shape[1])
                    outdata[:, :ch] = indata[:, :ch]
                outdata[:] *= self.master_volume
                return

            # 诊断: 均匀增益测试 → 验证 OLA 管线是否透明
            if self.uniform_test:
                avg_gain = (self._dsp_left._gains.mean() + self._dsp_right._gains.mean()) / 2
                if avg_gain > 0.5:
                    processed = np.clip(indata * (10.0 ** (avg_gain / 20.0)), -1, 1)
                else:
                    processed = indata
                outdata.fill(0)
                if processed.shape[1] == 1 and outdata.shape[1] >= 2:
                    outdata[:] = processed[:, :1]
                else:
                    ch = min(processed.shape[1], outdata.shape[1])
                    outdata[:, :ch] = processed[:, :ch]
                outdata[:] *= self.master_volume
                return

            # DSP 处理
            outdata.fill(0)
            in_ch = indata.shape[1]
            out_ch = outdata.shape[1]
            if in_ch == 1 and out_ch >= 2:
                ch_out = self._dsp_left.process_mono(indata[:, 0])
                outlen = min(len(ch_out), outdata.shape[0])
                outdata[:outlen, :] = ch_out[:outlen, None]
            else:
                for ch in range(min(in_ch, out_ch)):
                    dsp = self._dsp_left if ch == 0 else self._dsp_right
                    ch_in = indata[:, ch]
                    ch_out = dsp.process_mono(ch_in)
                    outlen = min(len(ch_out), outdata.shape[0])
                    outdata[:outlen, ch] = ch_out[:outlen]

            # 主音量
            outdata[:] *= self.master_volume

        except Exception as e:
            logger.error(f"DSP 回调异常: {e}")
            outdata.fill(0)

    def _start_volume_sync(self):
        """VB-Cable 模式下把 Windows 默认设备音量映射为 DSP 输出音量。"""
        self._stop_volume_sync()
        self._sync_master_volume_from_system()
        self._schedule_volume_sync()

    def _stop_volume_sync(self):
        timer = self._vol_sync_timer
        self._vol_sync_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _schedule_volume_sync(self):
        if not self._active:
            return
        timer = threading.Timer(self._vol_sync_interval, self._volume_sync_tick)
        timer.daemon = True
        self._vol_sync_timer = timer
        timer.start()

    def _volume_sync_tick(self):
        try:
            self._sync_master_volume_from_system()
        finally:
            self._schedule_volume_sync()

    def _sync_master_volume_from_system(self):
        try:
            muted, volume = vc.get_default_device_state()
            self.master_volume = 0.0 if muted else max(0.0, min(1.0, float(volume)))
        except Exception as e:
            logger.debug(f"系统音量同步失败: {e}")

    def _cleanup(self):
        self._stop_volume_sync()
        _write_state(False)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()


# ── 退出钩子注册 ──

def register_exit_handlers(stream: AudioStream):
    """注册所有进程级退出钩子 (Layer 2)"""

    @atexit.register
    def _atexit_restore():
        if stream.is_active:
            stream.stop()

    def _signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}, 正在退出...")
        stream.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Windows 控制台关闭事件
    if sys.platform == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
            def _console_handler(ctrl_type):
                stream.stop()
                return False

            kernel32.SetConsoleCtrlHandler(_console_handler, True)
        except Exception:
            pass
