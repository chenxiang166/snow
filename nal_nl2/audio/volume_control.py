"""系统音量控制 — 使用 pycaw (Python Core Audio Windows) 控制 Windows 音频端点

提供:
  - 静音/取消静音系统默认播放设备
  - 保存/恢复原始音量和静音状态
  - 获取当前设备状态

注意: pycaw 依赖 Windows COM, 仅在 Windows 上可用。
如果 pycaw 不可用, 所有操作降级为 no-op。
"""

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


def _get_default_endpoint():
    """获取系统默认扬声器端点"""
    if not _HAS_PYCAW:
        return None

    try:
        CoInitialize()
        # pycaw 新版 API: AudioUtilities.GetSpeakers() 返回 AudioDevice
        # 老版返回 MMDevice
        devices = AudioUtilities.GetSpeakers()

        # 兼容不同 pycaw 版本
        if hasattr(devices, 'Activate'):
            # 老版 MMDevice 接口
            interface = devices.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None
            )
        elif hasattr(devices, '_dev'):
            # 新版 AudioDevice: 通过内部 MMDevice
            interface = devices._dev.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None
            )
        else:
            # 尝试直接获取 endpoint volume
            logger.warning("无法识别的 pycaw 设备对象类型")
            return None

        return interface.QueryInterface(IAudioEndpointVolume)
    except Exception as e:
        logger.error(f"获取音频端点失败: {e}")
        return None
    finally:
        CoUninitialize()


def get_default_device_state() -> tuple[bool, float]:
    """
    获取默认设备的当前状态。

    返回:
        (is_muted: bool, master_volume: float 0.0~1.0)
    """
    if not _HAS_PYCAW:
        return False, 1.0

    try:
        endpoint = _get_default_endpoint()
        if endpoint is None:
            return False, 1.0
        mute = endpoint.GetMute()
        volume = endpoint.GetMasterVolumeLevelScalar()
        return mute, volume
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

    try:
        endpoint = _get_default_endpoint()
        if endpoint is None:
            return
        endpoint.SetMute(mute, None)
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

    try:
        endpoint = _get_default_endpoint()
        if endpoint is None:
            return
        volume = max(0.0, min(1.0, volume))
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

    try:
        endpoint = _get_default_endpoint()
        if endpoint is None:
            return
        endpoint.SetMasterVolumeLevelScalar(original_volume, None)
        endpoint.SetMute(original_mute, None)
        logger.info(f"设备已恢复: mute={original_mute}, vol={original_volume:.0%}")
    except Exception as e:
        logger.error(f"恢复设备失败: {e}")


def is_muted() -> bool:
    """检查默认设备是否静音"""
    if not _HAS_PYCAW:
        return False
    try:
        endpoint = _get_default_endpoint()
        if endpoint is None:
            return False
        return endpoint.GetMute()
    except Exception:
        return False


def get_volume() -> float:
    """获取默认设备主音量"""
    if not _HAS_PYCAW:
        return 1.0
    try:
        endpoint = _get_default_endpoint()
        if endpoint is None:
            return 1.0
        return endpoint.GetMasterVolumeLevelScalar()
    except Exception:
        return 1.0


def set_device_volume_by_id(portaudio_dev_id: int, volume: float):
    """设置指定 PortAudio 输出设备的音量"""
    if not _HAS_PYCAW:
        return
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        target_name = devices[portaudio_dev_id]['name'].lower()
        CoInitialize()
        try:
            all_devs = AudioUtilities.GetAllDevices()
            for dev in all_devs:
                try:
                    fid = dev.FriendlyName if hasattr(dev, 'FriendlyName') else dev.DeviceFriendlyName
                except:
                    continue
                if not fid or target_name[:15] not in fid.lower():
                    continue
                try:
                    interface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    ep = interface.QueryInterface(IAudioEndpointVolume)
                    ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, volume)), None)
                except:
                    pass
        finally:
            CoUninitialize()
    except Exception:
        pass


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
            all_devs = AudioUtilities.GetAllDevices()
            for dev in all_devs:
                try:
                    fid = dev.FriendlyName if hasattr(dev, 'FriendlyName') else dev.DeviceFriendlyName
                except:
                    continue
                if not fid:
                    continue
                fid_low = fid.lower()
                # 排除虚拟设备
                if 'cable' in fid_low or 'vb-audio' in fid_low or 'sonar' in fid_low or 'vad' in fid_low:
                    continue
                # 静音物理播放设备
                try:
                    interface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    ep = interface.QueryInterface(IAudioEndpointVolume)
                    ep.SetMute(True, None)
                except:
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
        target_name = devices[portaudio_dev_id]['name'].lower()
        CoInitialize()
        try:
            for dev in AudioUtilities.GetAllDevices():
                try:
                    fid = dev.FriendlyName if hasattr(dev, 'FriendlyName') else dev.DeviceFriendlyName
                except:
                    continue
                if not fid or target_name[:15] not in fid.lower():
                    continue
                interface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                ep = interface.QueryInterface(IAudioEndpointVolume)
                ep.SetMute(False, None)
                ep.SetMasterVolumeLevelScalar(1.0, None)
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
            all_devs = AudioUtilities.GetAllDevices()
            for dev in all_devs:
                try:
                    fid = dev.FriendlyName if hasattr(dev, 'FriendlyName') else dev.DeviceFriendlyName
                except:
                    continue
                if not fid:
                    continue
                fid_low = fid.lower()
                if 'cable' in fid_low or 'vb-audio' in fid_low:
                    continue
                try:
                    interface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    ep = interface.QueryInterface(IAudioEndpointVolume)
                    ep.SetMute(False, None)
                except:
                    pass
        finally:
            CoUninitialize()
    except Exception:
        pass
