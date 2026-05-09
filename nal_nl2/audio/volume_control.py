"""系统音量控制 — 使用 pycaw (Python Core Audio Windows) 控制 Windows 音频端点

提供:
  - 静音/取消静音系统默认播放设备
  - 保存/恢复原始音量和静音状态
  - 获取当前设备状态

注意: pycaw 依赖 Windows COM, 仅在 Windows 上可用。
如果 pycaw 不可用, 所有操作降级为 no-op。
"""

from contextlib import contextmanager
import logging
import sys

logger = logging.getLogger(__name__)

# 尝试导入 pycaw (仅 Windows)
_HAS_PYCAW = False
if sys.platform == 'win32':
    try:
        from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        _HAS_PYCAW = True
    except ImportError:
        logger.warning("pycaw 不可用，音量控制功能将跳过")


def _activate_endpoint(device):
    """兼容不同 pycaw 版本，从设备对象激活 IAudioEndpointVolume。"""
    if hasattr(device, 'Activate'):
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    elif hasattr(device, '_dev'):
        interface = device._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    else:
        raise TypeError(f"无法识别的 pycaw 设备对象类型: {type(device)!r}")
    return interface.QueryInterface(IAudioEndpointVolume)


@contextmanager
def _endpoint_context(device=None):
    """在 COM 初始化生命周期内提供 endpoint volume 接口。"""
    if not _HAS_PYCAW:
        yield None
        return

    initialized = False
    endpoint = None
    try:
        CoInitialize()
        initialized = True
        if device is None:
            device = AudioUtilities.GetSpeakers()
        endpoint = _activate_endpoint(device)
    except Exception as e:
        logger.error(f"获取音频端点失败: {e}")

    try:
        # The endpoint must be used before CoUninitialize; callers access it
        # inside the context manager body.
        yield endpoint
    finally:
        if initialized:
            try:
                CoUninitialize()
            except Exception:
                pass

def get_default_device_state() -> tuple[bool, float]:
    """
    获取默认设备的当前状态。

    返回:
        (is_muted: bool, master_volume: float 0.0~1.0)
    """
    if not _HAS_PYCAW:
        return False, 1.0

    with _endpoint_context() as endpoint:
        if endpoint is None:
            return False, 1.0
        try:
            mute = bool(endpoint.GetMute())
            volume = float(endpoint.GetMasterVolumeLevelScalar())
            return mute, max(0.0, min(1.0, volume))
        except Exception as e:
            logger.error(f"获取设备状态失败: {e}")
            return False, 1.0


def mute_default_device(mute: bool = True):
    """
    静音/取消静音系统默认播放设备。

    参数:
        mute: True=静音, False=取消静音
    """
    if not _HAS_PYCAW:
        logger.warning("pycaw 不可用，跳过静音操作")
        return

    with _endpoint_context() as endpoint:
        if endpoint is None:
            return
        try:
            endpoint.SetMute(bool(mute), None)
            logger.info(f"设备静音: {'是' if mute else '否'}")
        except Exception as e:
            logger.error(f"静音操作失败: {e}")


def set_default_device_volume(volume: float):
    """
    设置系统默认播放设备的主音量。

    参数:
        volume: 0.0 ~ 1.0
    """
    if not _HAS_PYCAW:
        return

    with _endpoint_context() as endpoint:
        if endpoint is None:
            return
        try:
            volume = max(0.0, min(1.0, float(volume)))
            endpoint.SetMasterVolumeLevelScalar(volume, None)
            logger.info(f"音量设置为: {volume:.0%}")
        except Exception as e:
            logger.error(f"音量设置失败: {e}")


def restore_default_device(original_mute: bool, original_volume: float):
    """
    恢复到原始状态。

    参数:
        original_mute:   原始静音状态
        original_volume: 原始音量 (0.0~1.0)
    """
    if not _HAS_PYCAW:
        return

    with _endpoint_context() as endpoint:
        if endpoint is None:
            return
        try:
            volume = max(0.0, min(1.0, float(original_volume)))
            endpoint.SetMasterVolumeLevelScalar(volume, None)
            endpoint.SetMute(bool(original_mute), None)
            logger.info(f"设备已恢复: mute={original_mute}, vol={volume:.0%}")
        except Exception as e:
            logger.error(f"恢复设备失败: {e}")


def is_muted() -> bool:
    """检查默认设备是否静音"""
    return get_default_device_state()[0]


def get_volume() -> float:
    """获取默认设备主音量"""
    return get_default_device_state()[1]


def _iter_output_endpoints():
    """迭代可激活的音频端点。调用方必须已初始化 COM。"""
    for dev in AudioUtilities.GetAllDevices():
        try:
            name = getattr(dev, 'FriendlyName', None) or getattr(dev, 'DeviceFriendlyName', None)
        except Exception:
            continue
        if name:
            yield dev, str(name)


def _matches_portaudio_device(portaudio_name: str, endpoint_name: str) -> bool:
    """用宽松字符串匹配 PortAudio 设备和 Windows endpoint 名称。"""
    pa = portaudio_name.lower()
    ep = endpoint_name.lower()
    if pa in ep or ep in pa:
        return True
    # PortAudio 名称常带接口/截断信息，使用前若干字符做兜底匹配。
    return pa[:15] in ep or ep[:15] in pa


def set_device_volume_by_id(portaudio_dev_id: int, volume: float):
    """设置指定 PortAudio 输出设备的音量"""
    if not _HAS_PYCAW:
        return
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        target_name = str(devices[portaudio_dev_id]['name'])
        volume = max(0.0, min(1.0, float(volume)))
        CoInitialize()
        try:
            for dev, endpoint_name in _iter_output_endpoints():
                if not _matches_portaudio_device(target_name, endpoint_name):
                    continue
                try:
                    ep = _activate_endpoint(dev)
                    ep.SetMasterVolumeLevelScalar(volume, None)
                except Exception as e:
                    logger.debug(f"设置设备音量失败 ({endpoint_name}): {e}")
        finally:
            CoUninitialize()
    except Exception as e:
        logger.debug(f"设置指定设备音量失败: {e}")


def mute_all_physical_outputs():
    """
    VB-Cable 模式: 静音所有物理播放设备 (排除虚拟设备)。
    系统音频只走 CABLE Input → 我们的 DSP → 物理输出。
    """
    if not _HAS_PYCAW:
        return
    try:
        CoInitialize()
        try:
            for dev, name in _iter_output_endpoints():
                name_low = name.lower()
                # 排除虚拟设备
                if 'cable' in name_low or 'vb-audio' in name_low or 'sonar' in name_low or 'vad' in name_low:
                    continue
                try:
                    ep = _activate_endpoint(dev)
                    ep.SetMute(True, None)
                except Exception:
                    pass
        finally:
            CoUninitialize()
    except Exception:
        pass


def unmute_device_by_id(portaudio_dev_id: int):
    """取消指定 PortAudio 输出设备的静音"""
    if not _HAS_PYCAW:
        return
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        target_name = str(devices[portaudio_dev_id]['name'])
        CoInitialize()
        try:
            for dev, endpoint_name in _iter_output_endpoints():
                if not _matches_portaudio_device(target_name, endpoint_name):
                    continue
                ep = _activate_endpoint(dev)
                ep.SetMute(False, None)
                # 不强制把物理设备音量拉到 100%，避免启动时突然过响。
        finally:
            CoUninitialize()
    except Exception:
        pass


def unmute_all_physical_outputs():
    """恢复所有物理播放设备"""
    if not _HAS_PYCAW:
        return
    try:
        CoInitialize()
        try:
            for dev, name in _iter_output_endpoints():
                name_low = name.lower()
                if 'cable' in name_low or 'vb-audio' in name_low:
                    continue
                try:
                    ep = _activate_endpoint(dev)
                    ep.SetMute(False, None)
                except Exception:
                    pass
        finally:
            CoUninitialize()
    except Exception:
        pass
