"""WASAPI Loopback 捕获管理 — 查找和配置音频设备"""

import logging
import sounddevice as sd

logger = logging.getLogger(__name__)


def _hostapi_score(hostapi_id: int) -> int:
    """低延迟优先级: WASAPI > WDM-KS > DirectSound > MME。"""
    try:
        name = sd.query_hostapis(hostapi_id)['name'].lower()
    except Exception:
        return 0
    if 'wasapi' in name:
        return 40
    if 'wdm' in name or 'ks' in name:
        return 30
    if 'directsound' in name:
        return 20
    if 'mme' in name:
        return 5
    return 10


def list_devices():
    """列出所有音频设备"""
    print(f"\n{'='*80}")
    print(f"{'ID':<4} {'Name':<40} {'In':>6} {'Out':>6} {'HostAPI':<20}")
    print(f"{'='*80}")

    for i, d in enumerate(sd.query_devices()):
        name = d['name']
        if len(name) > 38:
            name = name[:35] + '...'
        hostapi = sd.query_hostapis(d['hostapi'])['name'][:18]
        in_ch = d['max_input_channels']
        out_ch = d['max_output_channels']
        print(f"{i:<4} {name:<40} {in_ch:>6} {out_ch:>6} {hostapi:<20}")


def device_summary(device_id: int | None) -> str:
    """返回便于日志排查的设备摘要。"""
    if device_id is None:
        return "None"
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        d = devices[device_id]
        hostapi = hostapis[d['hostapi']]['name']
        return (
            f"[{device_id}] {d['name']} | "
            f"in={d['max_input_channels']} out={d['max_output_channels']} | "
            f"api={hostapi} | default_sr={d['default_samplerate']:.0f}"
        )
    except Exception as e:
        return f"[{device_id}] <读取失败: {e}>"


def log_device_inventory():
    """把目标机器的音频设备表写入日志。"""
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        logger.info("目标机器音频设备列表:")
        for i, d in enumerate(devices):
            hostapi = hostapis[d['hostapi']]['name']
            logger.info(
                "  [%s] %s | in=%s out=%s | api=%s | default_sr=%.0f",
                i,
                d['name'],
                d['max_input_channels'],
                d['max_output_channels'],
                hostapi,
                d['default_samplerate'],
            )
    except Exception as e:
        logger.warning(f"记录音频设备列表失败: {e}")


def find_loopback_device(hostapi_name: str = 'Windows WASAPI') -> int | None:
    """
    查找 WASAPI Loopback 设备。

    参数:
        hostapi_name: 主机 API 名称 (默认 'Windows WASAPI')

    返回:
        设备 ID, 如果找不到返回 None
    """
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()

    # 找到目标 hostapi
    target_api = None
    for i, api in enumerate(hostapis):
        if hostapi_name.lower() in api['name'].lower():
            target_api = i
            break

    # 查找该 hostapi 下的 loopback 设备
    for i, d in enumerate(devices):
        if d['hostapi'] == target_api:
            if 'loopback' in d['name'].lower() and d['max_input_channels'] > 0:
                return i

    # 备选: 尝试任何 hostapi 下的 loopback 设备
    for i, d in enumerate(devices):
        if 'loopback' in d['name'].lower() and d['max_input_channels'] > 0:
            return i

    return None


def find_stereo_mix_device() -> int | None:
    """
    查找立体声混音设备 (Stereo Mix) — 备选输入方案。
    立体声混音是 Realtek 声卡的"录制系统声音"功能。
    """
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        name = d['name'].lower()
        if d['max_input_channels'] > 0:
            if '立体声混音' in name or 'stereo mix' in name or 'wave out mix' in name:
                return i
    return None


def find_vb_cable_device() -> int | None:
    """
    查找 VB-Cable 虚拟音频设备 (录制端/CABLE Output)。

    VB-Cable 安装后在系统中注册两个端点:
      - CABLE Input  (播放设备): 系统音频流入此处
      - CABLE Output  (录制设备): 我们的程序从此处捕获
    """
    devices = sd.query_devices()
    candidates = []
    for i, d in enumerate(devices):
        name = d['name'].lower()
        if d['max_input_channels'] >= 2:
            if 'cable' in name or 'vb-audio' in name:
                candidates.append((_hostapi_score(d['hostapi']), i))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def find_input_device() -> tuple[int, str]:
    """
    查找可用的输入设备。
    优先级: VB-Cable > Loopback > Stereo Mix
    """
    vb = find_vb_cable_device()
    if vb is not None:
        return vb, 'vb_cable'

    loopback = find_loopback_device()
    if loopback is not None:
        return loopback, 'loopback'

    stereo_mix = find_stereo_mix_device()
    if stereo_mix is not None:
        return stereo_mix, 'stereo_mix'

    raise RuntimeError(
        "未找到可用的音频输入设备。\n\n"
        "推荐安装 VB-Cable: https://vb-audio.com/Cable/\n"
        "或启用立体声混音: Windows 声音设置 → 录制 → 右键启用「立体声混音」"
    )


def find_output_device_for_input(input_dev_id: int, input_mode: str) -> int | None:
    """
    根据输入设备找到合适的输出设备。

    优先找同 hostapi 的物理输出设备。VB-Cable 会优先选择 WASAPI 端点，
    避免落到 MME 带来明显延迟。
    """
    devices = sd.query_devices()
    input_device = devices[input_dev_id]
    hostapi = input_device['hostapi']

    def _is_physical(d):
        """判断是否为物理设备"""
        name = d['name'].lower()
        # 排除虚拟设备
        if 'cable' in name or 'vb-audio' in name:
            return False
        if 'sonar' in name or 'vad' in name or 'vb-' in name or 'voicemeeter' in name:
            return False
        if 'loopback' in name or 'digital' in name or 'spdif' in name:
            return False
        return True

    def _score(d):
        """设备优先级评分"""
        name = d['name'].lower()
        score = 0
        if 'headphone' in name or '耳机' in name:
            score += 10
        elif 'speaker' in name or '扬声器' in name:
            score += 8
        if 'realtek' in name:
            score += 3
        if _is_physical(d):
            score += 20
        # 惩罚 2nd/Secondary 输出
        if '2nd' in name or 'secondary' in name:
            score -= 25
        # 奖励 Primary
        if 'primary' in name:
            score += 5
        score += _hostapi_score(d['hostapi'])
        return score

    # 先找同 hostapi 的物理设备
    candidates = []
    for i, d in enumerate(devices):
        if d['hostapi'] != hostapi:
            continue
        if d['max_output_channels'] < 2:
            continue
        name = d['name'].lower()
        if not _is_physical(d):
            continue
        if '2nd' in name or 'secondary' in name:
            continue  # 跳过次要输出
        candidates.append((_score(d), i))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # 备选: 扩展到所有 hostapi
    all_candidates = []
    for i, d in enumerate(devices):
        if d['max_output_channels'] < 2:
            continue
        if not _is_physical(d):
            continue
        all_candidates.append((_score(d), i))

    if all_candidates:
        all_candidates.sort(reverse=True)
        return all_candidates[0][1]

    return None


def get_device_pair() -> tuple[int, int, str]:
    """
    自动查找输入 + 输出设备对。

    返回:
        (input_device_id, output_device_id, mode)
        mode 为 'loopback' 或 'stereo_mix'

    异常:
        RuntimeError: 未找到合适的设备
    """
    input_id, mode = find_input_device()
    output_id = find_output_device_for_input(input_id, mode)

    if output_id is None:
        raise RuntimeError(
            f"已找到输入设备 (ID={input_id}, mode={mode})，但未找到输出设备。\n"
            f"请确保扬声器/耳机已连接并启用。"
        )

    return input_id, output_id, mode


def print_device_info(input_id: int, output_id: int, mode: str = 'loopback'):
    """打印设备信息"""
    devices = sd.query_devices()
    inp = devices[input_id]
    out = devices[output_id]

    mode_label = {'loopback': 'WASAPI Loopback', 'stereo_mix': '立体声混音', 'vb_cable': 'VB-Cable 虚拟声卡'}.get(mode, mode)
    print(f"\n音频设备配置:")
    print(f"  模式: {mode_label}")
    print(f"  输入: [{input_id}] {inp['name']}")
    print(f"    Channels: {inp['max_input_channels']}, "
          f"Default SR: {inp['default_samplerate']:.0f} Hz")
    print(f"  输出: [{output_id}] {out['name']}")
    print(f"    Channels: {out['max_output_channels']}, "
          f"Default SR: {out['default_samplerate']:.0f} Hz")
    logger.info("音频设备配置: 模式=%s", mode_label)
    logger.info("  输入: %s", device_summary(input_id))
    logger.info("  输出: %s", device_summary(output_id))
