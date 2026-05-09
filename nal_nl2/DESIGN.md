# PC 音频个性化补偿 — 方案设计与底层原理

> **定位**：一款 Windows 系统级音频后处理软件。截取系统播放的所有声音（视频、音乐、游戏、浏览器等），根据个人听力图用 NAL-NL2 算法实时调整频响，再输出到耳机/音箱。本质是「架设在系统音频和物理设备之间的智能 EQ + 压缩器」。

---

## 一、系统架构总览

```
┌──────────────────────────────────────────────────────────────────────────┐
│                      PC 音频补偿软件 (Windows)                             │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  YouTube/游戏/音乐/浏览器 ─┐                                              │
│                          ▼                                              │
│               ┌─────────────────────┐                                   │
│               │  Windows 系统混音器   │  ← 所有应用的音频最终汇集于此        │
│               └─────────┬───────────┘                                   │
│                         ▼                                              │
│               ┌─────────────────────┐                                   │
│               │ WASAPI Loopback     │  ← 截取系统混音后的最终音频流        │
│               │ (共享模式, 48kHz)    │                                   │
│               └─────────┬───────────┘                                   │
│                         ▼                                              │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐   │
│  │  听力图输入   │──▶│ NAL-NL2 计算 │──▶│  实时 DSP 处理           │   │
│  │  (Audiogram) │   │ (增益+压缩比)  │   │  STFT → 频域增益 → ISTFT │   │
│  └──────────────┘   └──────────────┘   └───────────┬──────────────┘   │
│                                                    ▼                  │
│                                        ┌─────────────────────┐        │
│                                        │ WASAPI Render       │        │
│                                        │ 输出到耳机/音箱      │        │
│                                        └─────────────────────┘        │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

**核心模块说明：**

| 模块 | 职责 |
|------|------|
| **听力图输入** | 用户录入各频率 (125~8000 Hz) 的听阈 (dB HL)，支持左/右耳 |
| **NAL-NL2 计算器** | 根据听力图计算每个频带的**目标增益**与**压缩比** |
| **WASAPI Loopback 捕获** | 截取 Windows 混音器输出（所有应用音频的最终混合） |
| **实时 DSP 处理** | 分帧 STFT → 频域增益+WDRC → ISTFT |
| **WASAPI Render 输出** | 将处理后的音频送到耳机 / 音箱 |
| **UI 层** | Python + PyQt6，听力图录入 + 增益曲线预览 + 开关控制 |

---

## 二、NAL-NL2 算法原理

### 2.1 算法目标

NAL-NL2 由澳大利亚国家声学实验室 (NAL) 开发，是国际最主流的助听器验配公式之一。其核心目标是：

$$ \text{maximize speech intelligibility} \quad \text{while} \quad \text{loudness} \approx \text{normal hearing} $$

即在保证整体响度与正常听力者感知相当的约束下，最大化语音可懂度。

### 2.2 输入参数

| 参数 | 说明 | 典型值 |
|------|------|--------|
| $HTL(f)$ | 各频率听阈 (dB HL) | 0~120 dB HL |
| 频率 $f$ | 标准测听频率 | 125, 250, 500, 750, 1000, 1500, 2000, 3000, 4000, 6000, 8000 Hz |
| 助听器数量 | 单耳 / 双耳 | Binaural 额外-3dB |
| 性别 | 男 / 女 | 影响外耳共振修正 |
| 经验 | 新用户 / 老用户 | 老用户额外 ~3-5dB |
| 年龄 | 成人 / 儿童 | 儿童额外 ~5-8dB |

### 2.3 核心数学流程

#### Step 1 — 计算目标插入增益 (Insertion Gain)

对于每个频率 $f$，NAL-NL2 计算**目标 2cc 耦合腔增益**：

$$ G_{2cc}(f) = a(f) \cdot \text{HTL}(f) + b(f) + C_{binaural} + C_{gender} + C_{experience} + C_{age} $$

其中：
- $a(f)$：频率相关的增益系数（斜率），通常在 0.3~0.6 之间
- $b(f)$：频率相关的偏移量
- $C_{binaural}$：双耳修正 (-3 dB)
- $C_{gender}$：性别修正 (男性-2dB, 女性+2dB)
- $C_{experience}$：经验修正 (老用户+3dB)
- $C_{age}$：年龄修正 (儿童+5dB)

#### Step 2 — 压缩比计算 (Compression Ratio)

NAL-NL2 采用 **WDRC**（宽动态范围压缩），每个频带的压缩比 $CR(f)$：

$$ CR(f) = \max\left(1.0,\ \frac{HTL(f)}{K_{cr}} + 1.0\right) $$

典型压缩比范围：**1.0 : 1** (正常听力) 到 **3.0 : 1** (重度听损)。

| 听损程度 | 典型压缩比 |
|---------|-----------|
| 正常 (0-20 dB) | 1.0–1.3 |
| 轻度 (20-40 dB) | 1.3–1.6 |
| 中度 (40-60 dB) | 1.6–2.2 |
| 重度 (60-80 dB) | 2.2–3.0 |
| 极重度 (>80 dB) | 2.5–3.5 |

#### Step 3 — 增益-频率曲线平滑

原始逐频点增益可能不平滑，需做**跨频平滑**：

$$ G_{smooth}(f_i) = \frac{1}{3}\big[G(f_{i-1}) + G(f_i) + G(f_{i+1})\big] $$

#### Step 4 — 最大输出限制 (MPO)

为防止过度放大损伤残余听力，设置输出声压级上限：

$$ P_{max}(f) = \min\big(P_{UCL}(f),\ P_{safe}\big) $$

其中 $P_{UCL}(f)$ 是不适阈 (UCL, Uncomfortable Loudness Level)，$P_{safe}$ 通常设为 110 dB SPL。

### 2.4 完整增益计算伪代码

```python
def nal_nl2_calculate(htl: dict, config: dict) -> dict:
    """
    htl: {freq_hz: threshold_db_hl, ...}
    config: {num_aids: 1|2, gender: 'M'|'F', experience: 'new'|'experienced', age: 'adult'|'child'}
    return: {freq_hz: {'gain_db': float, 'compression_ratio': float, 'mpo_db_spl': float}, ...}
    """
    FREQS = [125, 250, 500, 750, 1000, 1500, 2000, 3000, 4000, 6000, 8000]

    # Step 1: 各频率基础增益
    raw_gains = {}
    for f in FREQS:
        ht = htl.get(f, 0)
        a = _gain_coefficient_a(f)      # 频率相关增益系数
        b = _gain_coefficient_b(f)      # 频率相关偏移
        raw_gain = a * ht + b

        # 双耳修正
        if config['num_aids'] == 2:
            raw_gain -= 3

        # 性别修正
        if config['gender'] == 'M':
            raw_gain -= 2
        elif config['gender'] == 'F':
            raw_gain += 2

        # 经验修正
        if config['experience'] == 'experienced':
            raw_gain += 3

        # 年龄修正
        if config['age'] == 'child':
            raw_gain += 5

        raw_gains[f] = max(0, raw_gain)  # 增益不低于 0dB

    # Step 2: 平滑处理
    gains = _cross_frequency_smoothing(raw_gains, FREQS)

    # Step 3: 计算压缩比和 MPO
    result = {}
    for f in FREQS:
        ht = htl.get(f, 0)
        result[f] = {
            'gain_db': round(gains[f], 1),
            'compression_ratio': round(_calc_cr(ht), 2),
            'mpo_db_spl': round(min(_estimate_ucl(f, ht), 110), 1)
        }

    return result
```

---

## 三、实时音频处理流水线

### 3.1 信号流 (WASAPI Loopback → DSP → Output)

```
                    ┌──────── Window 系统音频混音器 ────────┐
                    │  Chrome   MediaPlayer   Game    ...   │
                    │    │         │           │          │ │
                    │    └─────────┴─────┬─────┘          │ │
                    │                   ▼                 │ │
                    │          Device Graph (WASAPI)      │ │
                    └───────────────────┬─────────────────┘
                                        │ 系统混音后的 PCM 流
                                        ▼
┌────────────┐     ┌──────────────┐     ┌────────────────┐     ┌──────────────┐     ┌────────────┐
│  WASAPI    │     │  分帧 + 加窗  │     │  N 点 FFT      │     │  频域增益     │     │  IFFT +    │
│  Loopback  │────▶│  Frame: 20ms │────▶│  频域分解       │────▶│  + WDRC 压缩  │────▶│  OLA 合成  │────▶ 耳机/音箱
│  48kHz PCM │     │  Hann窗      │     │  N=2048        │     │  (NAL-NL2)    │     │            │
└────────────┘     └──────────────┘     └────────────────┘     └──────────────┘     └────────────┘
```

> **关键区别**：输入来源是 WASAPI Loopback（系统混音器的最终输出），不是麦克风。所有应用的声音——视频、音乐、游戏、系统提示音——都会经过这个流水线。

### 3.2 关键参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 采样率 | 48 kHz | 与系统默认采样率一致，避免重采样 |
| 帧长 | 20 ms = 960 samples | 平衡延迟与频率分辨率 |
| FFT 点数 | N = 2048 | 2 的幂，覆盖完整帧 |
| 重叠率 | 50% | OLA (Overlap-Add) 合成 |
| 窗函数 | Hann | 减少频谱泄漏 |
| 频带数 | 10 个 | 按标准测听频率划分 |

### 3.3 频带划分

| 频带 | 中心频率 (Hz) | FFT Bin 范围 (48kHz, N=2048) |
|------|--------------|-----------------------------|
| Band 0 | 125 | bin 4–7 |
| Band 1 | 250 | bin 8–15 |
| Band 2 | 500 | bin 16–27 |
| Band 3 | 750 | bin 28–37 |
| Band 4 | 1000 | bin 38–48 |
| Band 5 | 1500 | bin 49–69 |
| Band 6 | 2000 | bin 70–90 |
| Band 7 | 3000 | bin 91–133 |
| Band 8 | 4000 | bin 134–175 |
| Band 9 | 6000 | bin 176–261 |
| Band 10 | 8000 | bin 262–347 |

### 3.4 WDRC 压缩实现

每个频带的增益实时计算（以 dB 为单位）：

$$ G_{out}(f) = \begin{cases} G_{target}(f), & P_{in}(f) \leq T_k(f) \\ G_{target}(f) - \left(1 - \frac{1}{CR(f)}\right) \cdot (P_{in}(f) - T_k(f)), & P_{in}(f) > T_k(f) \end{cases} $$

其中 $T_k$ 是压缩拐点 (knee point)，设为 50 dB SPL。

```python
def wdrc_apply(input_db: float, target_gain_db: float, cr: float, knee_db: float = 50) -> float:
    """
    对单频带应用 WDRC 压缩
    input_db: 输入信号功率 (dB)
    target_gain_db: NAL-NL2 目标增益 (dB)
    cr: 压缩比
    knee_db: 压缩拐点 (dB)
    """
    if input_db <= knee_db:
        return input_db + target_gain_db     # 线性区
    else:
        excess = input_db - knee_db
        return knee_db + excess / cr + target_gain_db  # 压缩区
```

当输入信号很大时（如电影爆炸场面），压缩器会降低增益，保护听力。

---

## 三-A、音频截获与输出 — 方案全景对比

要实现「截获系统所有音频 → 处理 → 输出」，在 Windows 上有 **5 种架构路线**。

### 方案矩阵

```
                          ┌─ 方案 A: WASAPI Loopback (当前选择)
                          │
  PC 音频截获方案 ────────┼─ 方案 B: Virtual Audio Cable (VB-Cable)
                          │
                          ├─ 方案 C: Windows APO (Audio Processing Object)
                          │
                          ├─ 方案 D: KS Filter Driver (内核流过滤)
                          │
                          └─ 方案 E: 应用级 Hook (API Hook)
```

### 详细对比

#### 方案 A — WASAPI Loopback + 独立输出（当前选择 ✅）

```
┌──────────────────────────────────────────────────────┐
│                  Windows Audio Engine                │
│  ┌─────┐ ┌─────┐ ┌─────┐                            │
│  │ App │ │ App │ │ App │  所有应用                    │
│  └──┬──┘ └──┬──┘ └──┬──┘                            │
│     └───────┼───────┘                                │
│             ▼                                        │
│        ┌─────────┐                                   │
│        │ 混音器   │                                   │
│        └────┬────┘                                   │
│       ┌─────┴─────┐                                  │
│       ▼           ▼                                  │
│   物理输出    Loopback端点 ──▶ [我们的DSP] ──▶ 耳机  │
│   (静音掉)                                              │
└──────────────────────────────────────────────────────┘
```

| 维度 | 评价 |
|------|------|
| **延迟** | ⭐⭐⭐⭐ ~30ms，满足要求 |
| **实现难度** | ⭐⭐⭐⭐⭐ 极低，sounddevice 几行代码 |
| **覆盖面** | ⭐⭐⭐⭐⭐ 所有应用混音后的最终流 |
| **稳定性** | ⭐⭐⭐⭐ 依赖 PortAudio，久经考验 |
| **侵入性** | ⭐⭐⭐ 需要手动静音系统音量 |
| **打包分发** | ⭐⭐⭐⭐ 纯 Python + 依赖，PyInstaller 即可 |

**原理**：WASAPI Loopback 是 Windows Vista 起内置的功能，可以把混音后的最终音频流「复制一份」给应用程序读取。本方案读取这份副本，处理后通过另一个 WASAPI 输出设备播放。因为系统默认设备也在同时播放原始音频，所以必须**将其静音**。

**优点**：
- 无需安装任何虚拟设备驱动
- 无需修改系统配置
- 代码量极少（sounddevice 自动处理 PortAudio + WASAPI 细节）

**缺点**：
- 需要手动管理系统静音（可通过 pycaw 自动化）
- 端到端延迟 ~30ms，看视频时口型基本同步，但对电竞等高精度场景可能有感
- Loopback 捕获和输出必须是同一 WASAPI hostapi（通常是同一个声卡），跨设备需要额外处理

---

#### 方案 B — Virtual Audio Cable（VB-Cable / Voicemeeter）

```
┌─────┐ ┌─────┐ ┌─────┐
│ App │ │ App │ │ App │
└──┬──┘ └──┬──┘ └──┬──┘
   └───────┼───────┘
           ▼
    ┌─────────────┐
    │ 系统默认设备  │  ← 设为 VB-Cable (虚拟扬声器)
    │  = VB-Cable  │
    └──────┬──────┘
           ▼ 虚拟设备输出
    ┌─────────────┐
    │ 我们的 DSP   │  ← 从 VB-Cable 的录制端点捕获
    └──────┬──────┘
           ▼
    ┌─────────────┐
    │ 物理耳机     │
    └─────────────┘
```

| 维度 | 评价 |
|------|------|
| **延迟** | ⭐⭐⭐ ~40-60ms（多一层虚拟设备） |
| **实现难度** | ⭐⭐⭐⭐ 较简单 |
| **覆盖面** | ⭐⭐⭐⭐⭐ 系统级，无遗漏 |
| **稳定性** | ⭐⭐⭐ 依赖第三方驱动 |
| **侵入性** | ⭐⭐ 需要安装驱动 + 切换默认设备 |
| **打包分发** | ⭐⭐ 用户需额外安装 VB-Cable |

**原理**：VB-Cable 是一个虚拟音频驱动程序，在系统中注册为「扬声器」和「麦克风」对。将系统默认播放设备设为 VB-Cable 扬声器端，所有应用音频流入虚拟设备。DSP 程序从 VB-Cable 麦克风端读取数据，处理后输出到物理耳机。

**优点**：
- 架构清晰，输入输出天然分离
- 不需要手动静音系统设备

**缺点**：
- **用户必须安装 VB-Cable 驱动**（需要管理员权限）
- 多一层虚拟设备带来额外延迟
- VB-Cable 免费版有音质限制
- 分发时需要引导用户安装依赖

---

#### 方案 C — Windows APO（Audio Processing Object）

```
┌──────────────────────────────────────────┐
│         Windows Audio Engine             │
│                                          │
│  App → 混音器 → [我们的APO] → 物理输出   │
│                  ↑                      │
│           系统原生DSP注入点               │
└──────────────────────────────────────────┘
```

| 维度 | 评价 |
|------|------|
| **延迟** | ⭐⭐⭐⭐⭐ <5ms（零额外延迟） |
| **实现难度** | ⭐ 极高 |
| **覆盖面** | ⭐⭐⭐⭐⭐ 完美系统级 |
| **稳定性** | ⭐⭐⭐⭐ 系统原生机制 |
| **侵入性** | ⭐ 需要 .inf 安装 + 驱动签名 |
| **打包分发** | ⭐ 最复杂 |

**原理**：Windows 从 Vista 开始支持 APO（音频处理对象），允许第三方 DSP 以 COM DLL 的形式插入到音频引擎的处理链中。APO 在系统混音之后、DAC 之前运行，是 Windows 原生支持的「官方注入点」。

APO 有三种类型：
- **SFX APO**（Stream Effect）：每应用流处理
- **MFX APO**（Mode Effect）：设备模式处理
- **EFX APO**（Endpoint Effect）：**端点（设备）级处理 ← 我们需要这个**

**优点**：
- **零额外延迟**：APO 直接在音频引擎内部执行，不引入任何缓冲
- 真正的系统级方案，无需用户做额外操作
- 对所有应用透明

**缺点**：
- 必须用 **C++** 编写 COM DLL
- 需要 **.inf 安装脚本** 注册到特定音频设备
- 需要 **代码签名证书**（EV 证书，~$300/年）或强制测试模式
- 调试极其困难（崩溃会连带音频服务重启）
- 开发中出错可能导致系统无声
- 分发需要管理员权限安装

> 💡 **Swap APO** 是新方案：Windows 11 22H2+ 支持用户态替换 APO，免去 .inf 安装。但限制较多且需要商店签名。

---

#### 方案 D — KS Filter Driver（内核流过滤）

```
┌───────────────────────────────────────┐
│         Kernel Streaming Pipeline     │
│                                       │
│  App → 混音器 → [KS Filter] → 驱动   │
│                  ↑                   │
│            内核驱动注入               │
└───────────────────────────────────────┘
```

| 维度 | 评价 |
|------|------|
| **延迟** | ⭐⭐⭐⭐⭐ 接近零 |
| **实现难度** | ⭐ 最极端的难度 |
| **覆盖面** | ⭐⭐⭐⭐⭐ |
| **稳定性** | ⭐⭐ 一个 bug = 蓝屏 |
| **侵入性** | ⭐ 需要签名内核驱动 |
| **打包分发** | ⭐ 几乎不可行 |

**结论**：仅在商业声卡驱动中使用。个人项目**强烈不推荐**。

---

#### 方案 E — API Hook / DLL 注入

| 维度 | 评价 |
|------|------|
| **延迟** | ⭐⭐⭐⭐ |
| **实现难度** | ⭐⭐ 中等偏难 |
| **覆盖面** | ⭐⭐ 每个进程单独注入，容易遗漏 |
| **稳定性** | ⭐⭐ 极易触发反作弊/安全软件 |
| **侵入性** | ⭐ 杀毒软件会报警 |

**结论**：不适用于此场景。覆盖面不完整，且极易被安全软件拦截。

---

### 综合对比一览

| | A: Loopback | B: VB-Cable | C: APO | D: Kernel | E: Hook |
|---|---|---|---|---|---|
| **延迟** | ~30ms | ~50ms | <5ms | <5ms | ~20ms |
| **开发难度** | 极低 | 低 | 极高 | 极高 | 中高 |
| **覆盖面** | 全部 | 全部 | 全部 | 全部 | 部分 |
| **需安装驱动** | ❌ | ✅ | ✅ | ✅ | ❌ |
| **需代码签名** | ❌ | ❌ | ✅ | ✅ | ❌ |
| **分发便利性** | 极好 | 中 | 差 | 极差 | 中 |
| **适合个人项目** | ✅✅✅ | ✅✅ | ❌ | ❌ | ❌ |

### 我们的选择：方案 A (WASAPI Loopback)

**选择理由：**

1. **30ms 延迟在目标场景完全够用**：看视频（口型同步阈值 ~50ms）、听音乐、玩游戏时延迟感知远低于阈值
2. **开发效率碾压其他方案**：Python + sounddevice 即可，不需要 C++、驱动签名、inf 安装
3. **用户零门槛**：无需安装任何驱动或切换设备，打开软件即可使用
4. **未来可升级到 APO**：Loopback 方案验证算法效果后，如有必要再移植到 C++ APO，降低风险
5. **分发简单**：`pyinstaller` 一键打包为 exe

**唯一需要处理的问题**：系统默认设备会同时播放原始音频（双重输出），解决方法：

```python
# 从 pycaw 静音系统主音量 → 用户只听到处理后的声音
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

devices = AudioUtilities.GetSpeakers()
interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
volume = interface.QueryInterface(IAudioEndpointVolume)
volume.SetMute(True, None)   # 静音原始输出
# → 仅通过我们的 Stream 输出处理后的音频
```

> 📌 **后续升级路径**：如果对延迟有极限要求，可以在 Loopback 方案验证完算法后，将核心 DSP 移植为 C++ APO。算法和参数体系完全可复用。

### 3.5 总延迟估算

| 阶段 | 延迟 | 说明 |
|------|------|------|
| WASAPI 环回捕获缓冲 | ~10 ms | PortAudio 与系统协商 |
| 帧采集 (10ms half-frame) | ~10 ms | 使用 50% 重叠，有效延迟减半 |
| FFT + 处理 + IFFT | ~2 ms | NumPy + Numba JIT |
| WASAPI 输出缓冲 | ~10 ms | PortAudio 输出缓冲 |
| **总计** | **~32 ms** | 端到端延迟远低于口型同步阈值 (50ms) |

---

## 四、Windows 平台技术方案

### 4.1 技术栈

| 方案 | 优点 | 缺点 |
|------|------|------|
| **Python + sounddevice (推荐)** | 开发效率高，PortAudio 底层封装好 | 需注意 GIL |
| C++ / Rust + WASAPI 直调 | 延迟最低 | 开发周期长 |
| 虚拟声卡驱动 (APO) | 系统级，无额外延迟 | 需要驱动签名，开发复杂 |

**推荐方案**：Python + sounddevice + NumPy/Numba，适合快速实现和迭代。

### 4.2 依赖库

```
sounddevice     — PortAudio 封装，支持 WASAPI Loopback + Render
numpy           — FFT + 向量化数值计算
scipy           — 信号处理辅助
numba           — JIT 编译加速 DSP 热点函数
matplotlib      — 增益曲线可视化
PyQt6           — GUI 界面框架
```

### 4.3 WASAPI Loopback 捕获原理

```
                         Windows Audio Engine
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  ┌─────┐  ┌─────┐  ┌─────┐                                     │
│  │ App1│  │ App2│  │ App3│   应用程序各自输出音频                  │
│  └──┬──┘  └──┬──┘  └──┬──┘                                     │
│     │        │        │                                         │
│     └────────┼────────┘                                         │
│              ▼                                                   │
│     ┌────────────────┐                                          │
│     │   混音器 (Mixer)│   ← 所有应用音频混合为最终 PCM 流         │
│     └───────┬────────┘                                          │
│             │                                                    │
│      ┌──────┴──────┐                                            │
│      ▼              ▼                                           │
│  ┌─────────┐  ┌───────────────┐                                 │
│  │ 物理输出 │  │ Loopback 端点 │  ← 我们从此处捕获                 │
│  │ (扬声器) │  │ (虚拟捕获端点) │                                 │
│  └─────────┘  └───────┬───────┘                                 │
│                       │                                          │
└───────────────────────┼──────────────────────────────────────────┘
                        ▼
              我们的 DSP 处理程序
                        │
                        ▼
              ┌─────────────────┐
              │ 物理耳机 / 音箱  │  ← 处理后的声音从这里播出
              └─────────────────┘
```

**关键点：**
- Loopback 模式捕获的是混音后数据，包含**所有系统声音**
- 采样率和声道数必须与输出设备匹配（通常 48kHz 立体声）
- 运行期间应将系统主音量设为 100%，用我们的 DSP 控制最终增益
- 需要**将系统默认播放设备静音**，避免输出原始+处理后双重声音

### 4.4 sounddevice Loopback 核心代码

```python
import sounddevice as sd

# 查找 Loopback 设备
devices = sd.query_devices()
for i, d in enumerate(devices):
    if d['max_input_channels'] > 0 and 'Loopback' in d['name']:
        loopback_dev = i
    if d['max_output_channels'] > 0 and d['hostapi'] == devices[loopback_dev]['hostapi']:
        output_dev = i  # 同一 hostapi 下的输出设备

# 打开全双工流：输入端 = Loopback，输出端 = 物理设备
stream = sd.Stream(
    samplerate=48000,
    blocksize=960,
    device=(loopback_dev, output_dev),  # (捕获=环回, 播放=耳机)
    channels=(2, 2),                     # 立体声输入 → 立体声输出
    dtype='float32',
    latency='low',
    callback=audio_callback
)
```

### 4.5 使用步骤（用户侧）

1. 打开本软件，录入听力图
2. 点击 **「开始」**，软件自动完成：
   - 将系统默认播放设备**静音**（或用脚本调低其音量）
   - 打开 WASAPI Loopback 捕获
   - 启动 DSP 流水线
   - 通过目标输出设备播放处理后的音频
3. 点击 **「停止」**，恢复系统默认设置

---

## 五、UI 设计

```
┌─────────────────────────────────────────────────────────────────┐
│  PC 音频补偿 - 基于 NAL-NL2                                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌─────────────────────── 听力图输入 ───────────────────────┐  │
│   │                                                           │  │
│   │  频率(Hz) 125  250  500  750 1000 1500 2000 3000 4000 6000 8000 │
│   │  ──────── ───  ───  ───  ─── ──── ──── ──── ──── ──── ──── ──── │
│   │  左耳(dB) [ 0] [ 0] [10] [15] [20] [30] [40] [50] [55] [60] [65]│
│   │  右耳(dB) [ 0] [ 0] [ 5] [10] [15] [25] [35] [45] [50] [55] [60]│
│   │                                                           │  │
│   │  ┌─────────────────────────────────────┐                  │  │
│   │  │        ▲ 增益曲线 (实时预览)         │                  │  │
│   │  │  40 ┤                    ▄▄▄▄      │                  │  │
│   │  │  30 ┤              ▄▄▄▄▄           │                  │  │
│   │  │  20 ┤        ▄▄▄▄▄                 │                  │  │
│   │  │  10 ┤   ▄▄▄▄▄                       │                  │  │
│   │  │   0 ┼───┬───┬───┬───┬───┬───┬───  │                  │  │
│   │  │     125 500 1k  2k  3k  4k  6k 8k │                  │  │
│   │  └─────────────────────────────────────┘                  │  │
│   │                                                           │  │
│   │  ○ 单耳  ● 双耳  │  ● 男  ○ 女  │  ● 新用户  ○ 老用户  │  │
│   └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│   ┌───────────────────────────────────────────────────────────┐  │
│   │  系统音频设备：                                           │  │
│   │  输入(环回): Speakers (Realtek)   输出: 耳机 (USB)       │  │
│   │                                                            │  │
│   │  [ ▶ 开始处理 ]  [ ■ 停止 ]   状态: ● 运行中              │  │
│   │  延迟: 32ms   CPU: 3.1%   音量: ━━━━━━◉━━━ 80%           │  │
│   └───────────────────────────────────────────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**UI 交互说明：**
- 听力图可用滑块或直接输入数值
- 增益曲线实时更新，修改任何参数即刻反映
- **「开始处理」**自动静音系统主音量，开启 Loopback → DSP → 输出
- **「停止」**恢复系统音量，释放音频设备
- 状态栏显示实时延迟和 CPU 占用

---

## 六、项目目录结构

```
nal_nl2/
├── DESIGN.md              ← 本文档
├── requirements.txt       ← Python 依赖
├── main.py                ← 主入口 & GUI
├── nal_nl2/
│   ├── __init__.py
│   ├── prescription.py    ← NAL-NL2 增益计算核心
│   ├── dsp_engine.py      ← 实时音频处理 (STFT + WDRC)
│   ├── filterbank.py      ← 多频带滤波器组
│   └── constants.py       ← 常量定义 (频率表、系数表等)
├── audio/
│   ├── __init__.py
│   ├── loopback.py        ← WASAPI Loopback 捕获管理
│   ├── stream.py          ← 全双工音频流封装
│   └── volume_control.py  ← 系统音量控制 (pycaw)
├── ui/
│   ├── __init__.py
│   ├── main_window.py     ← 主窗口
│   ├── audiogram_widget.py← 听力图输入控件
│   └── gain_plot.py       ← 增益曲线图
└── tests/
    ├── test_prescription.py
    └── test_dsp.py
```

---

## 七、关键技术细节

### 7.1 增益系数表 $a(f)$ 和 $b(f)$

NAL-NL2 的增益系数是通过大量临床数据拟合得到的。下表为简化近似值：

| 频率 (Hz) | a(f) (斜率) | b(f) (截距 dB) |
|-----------|------------|---------------|
| 125 | 0.35 | -2.0 |
| 250 | 0.38 | -1.5 |
| 500 | 0.42 | -1.0 |
| 750 | 0.45 | -0.5 |
| 1000 | 0.48 | 0.0 |
| 1500 | 0.50 | +1.0 |
| 2000 | 0.52 | +2.0 |
| 3000 | 0.52 | +3.0 |
| 4000 | 0.50 | +3.5 |
| 6000 | 0.45 | +4.0 |
| 8000 | 0.40 | +4.5 |

> ⚠️ 上表为近似值。完整的 NAL-NL2 公式包含更复杂的频率相关因子，上述值可作为初始实现，后续可校准。

### 7.2 响度模型简化

完整 NAL-NL2 使用 Moore & Glasberg 响度模型计算特定响度 (specific loudness)。在我们的简化实现中：

- 使用 **dB SPL → Phon → Sone** 的简化映射
- 参考等响曲线 (ISO 226:2003) 进行频率加权
- 但这部分在初版中可以省略，先用纯增益+压缩替代

### 7.3 避免双重输出

Loopback 模式下，**系统默认设备仍在播放原始音频**。必须处理：

1. **静音系统主音量**（推荐）：用 `pycaw` 将系统默认设备静音
2. **或者**：将系统默认设备设为虚拟设备（VB-Cable），本软件从其 Loopback 捕获

```python
# 使用 pycaw 控制系统音量
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

sessions = AudioUtilities.GetAllSessions()
for session in sessions:
    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
    volume.SetMasterVolume(0, None)   # 静音特定应用
```

> 更简单的方式：直接控制系统默认设备的静音状态。

### 7.4 程序退出与音频恢复 ⚠️ 关键工程问题

**问题**：程序运行时静音了系统默认设备。如果异常退出（崩溃、杀进程、断电），静音状态不会被恢复，用户将**彻底听不到任何系统声音**。

#### 退出场景矩阵

| 退出方式 | 能否执行恢复代码 | 风险等级 |
|----------|:-------------:|:------:|
| 用户点击「停止」按钮 | ✅ 必然能 | 🟢 安全 |
| 关闭窗口 (WM_CLOSE) | ✅ Qt closeEvent | 🟢 安全 |
| `Ctrl+C` 在终端 | ✅ KeyboardInterrupt / signal | 🟢 安全 |
| Python 未捕获异常 | ✅ try/finally + atexit | 🟡 基本安全 |
| 任务管理器「结束任务」 | ❌ atexit 不触发 | 🔴 高风险 |
| `taskkill /F` 强杀 | ❌ 无机会执行 | 🔴 高风险 |
| 进程自身 segfault | ❌ 进程立即终止 | 🔴 高风险 |
| 系统蓝屏 / 断电 | ❌ 物理中断 | 🟡 重启后自动恢复 |

#### 三层防护架构

```
                    ┌──────────────────────────┐
   Layer 1          │  正常退出路径              │
  (代码内恢复)       │  closeEvent / try-finally │
                    │  signal(SIGINT/SIGTERM)   │
                    └──────────┬───────────────┘
                               │ 覆盖 80% 退出场景
                               ▼
                    ┌──────────────────────────┐
   Layer 2          │  进程退出钩子              │
  (OS 级钩子)        │  atexit.register()        │
                    │  SetConsoleCtrlHandler    │
                    └──────────┬───────────────┘
                               │ 覆盖 95% 退出场景
                               ▼
                    ┌──────────────────────────┐
   Layer 3          │  脏状态检测 + 自动修复      │
  (启动时兜底)       │  启动时检查状态文件         │
                    │  无条件 unmute + 恢复音量   │
                    └──────────────────────────┘
                               │ 覆盖 100% 场景
                               │ (下次启动时修复)
```

#### Layer 1: 代码内正常恢复

```python
import signal
import sys

class AudioManager:
    def __init__(self):
        self._original_mute = None
        self._original_volume = None
        self._active = False

    def start(self):
        # 1. 保存原始状态
        endpoint = get_default_endpoint()
        self._original_mute = endpoint.GetMute()
        self._original_volume = endpoint.GetMasterVolumeLevelScalar()

        # 2. 静音系统默认设备
        endpoint.SetMute(True, None)
        self._active = True

        # 3. 写入状态文件 (Layer 3 使用)
        self._write_state_file(active=True,
                               mute=self._original_mute,
                               volume=self._original_volume)

        # 4. 启动音频流
        self.stream.start()

    def stop(self):
        """正常停止 — 恢复一切"""
        self._restore()
        self._write_state_file(active=False)
        self._active = False
```

#### Layer 2: 进程级退出钩子

```python
import atexit
import signal

def _register_exit_handlers(audio_mgr):
    """注册所有可能的退出钩子"""

    # Python 正常退出时
    atexit.register(audio_mgr._restore)

    # Ctrl+C / SIGTERM
    signal.signal(signal.SIGINT, lambda *a: audio_mgr._restore() or sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *a: audio_mgr._restore() or sys.exit(0))

    # Windows 控制台关闭事件
    if sys.platform == 'win32':
        import ctypes
        kernel32 = ctypes.windll.kernel32
        def console_handler(ctrl_type):
            audio_mgr._restore()
            return False  # 让系统继续处理
        kernel32.SetConsoleCtrlHandler(
            ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)(console_handler), True)
```

#### Layer 3: 启动时自动修复（兜底）

```python
import os, json

STATE_FILE = os.path.join(os.path.expanduser('~'), '.pc_audio_comp_state.json')

def _write_state_file(self, active: bool, mute=None, volume=None):
    with open(STATE_FILE, 'w') as f:
        json.dump({
            'active': active,
            'original_mute': mute,
            'original_volume': volume,
            'pid': os.getpid(),
            'timestamp': time.time()
        }, f)

def check_and_recover_on_startup():
    """
    程序启动时调用。
    如果上次退出是 dirty 状态，自动恢复音频。
    """
    if not os.path.exists(STATE_FILE):
        return  # 首次运行

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except:
        return

    if state.get('active'):
        # 检查该 PID 是否还活着
        pid = state.get('pid', 0)
        try:
            os.kill(pid, 0)  # 信号 0 = 仅检查进程是否存在
            # 进程还活着 — 说明是旧实例，不恢复
            return
        except OSError:
            # 进程已死 — dirty 退出，恢复音频！
            endpoint = get_default_endpoint()
            endpoint.SetMute(state.get('original_mute', False), None)
            if state.get('original_volume') is not None:
                endpoint.SetMasterVolumeLevelScalar(state['original_volume'], None)
            # 清理状态文件
            os.remove(STATE_FILE)
```

#### 完整流程示意

```
程序启动
  │
  ├─▶ check_and_recover_on_startup()
  │     ├─ 状态文件存在 & active=True & 旧进程已死？
  │     │    └─ YES → 恢复原始静音/音量 + 清理状态文件
  │     └─ NO → 跳过
  │
  ├─▶ 用户点击「开始」
  │     ├─ 保存原始状态 → 写入状态文件
  │     ├─ 静音系统默认设备
  │     └─ 注册 atexit + signal 钩子
  │
  ├─▶ 正常运行...
  │
  └─▶ 用户点击「停止」(或正常退出)
        ├─ 恢复原始静音/音量
        ├─ 写入 active=False 状态文件
        └─ 清理资源

--- 如果崩溃/强杀 ---

下次启动
  │
  └─▶ check_and_recover_on_startup()
        └─ 检测到 dirty 状态 → 自动恢复！
```

#### 在原型中的应用

```python
# main.py
import atexit, signal, sys, os, json

def safe_exit(stream, endpoint, original_state):
    """无论如何退出都要调用的恢复函数"""
    try:
        stream.stop()
        stream.close()
    except: pass
    try:
        endpoint.SetMute(original_state['mute'], None)
        endpoint.SetMasterVolumeLevelScalar(original_state['volume'], None)
    except: pass
    try:
        os.remove(STATE_FILE)
    except: pass

def main():
    # Layer 3: 启动时修复脏状态
    check_and_recover_on_startup()

    # 正常流程...
    original = {'mute': endpoint.GetMute(), 'volume': endpoint.GetMasterVolumeLevelScalar()}
    write_state_file(active=True, **original)

    # Layer 2: 注册所有钩子
    atexit.register(lambda: safe_exit(stream, endpoint, original))
    signal.signal(signal.SIGINT, lambda *a: (safe_exit(...), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (safe_exit(...), sys.exit(0)))

    try:
        stream.start()
        app.exec()   # Qt 事件循环
    finally:
        # Layer 1: 正常退出
        safe_exit(stream, endpoint, original)
```

#### 恢复机制总结

| 退出场景 | 恢复依靠 | 用户影响 |
|----------|---------|---------|
| 点击停止 | Layer 1: try/finally | 立即恢复 |
| 关闭窗口 | Layer 1: closeEvent | 立即恢复 |
| Ctrl+C | Layer 2: signal | 立即恢复 |
| 未捕获异常 | Layer 1+2: atexit | 立即恢复 |
| 任务管理器强杀 | Layer 3: 下次启动恢复 | 静音到下次打开软件 |
| 断电/蓝屏 | Layer 3: 下次启动恢复 + Windows 自身 | 重启后自动正常 |

> ⚡ **核心原则**：三层防护确保 `SetMute(False)` 最终一定会被调用。最坏情况是用户需要重新打开一次本软件，它会自动修复。不会出现永久无声。

---

## 八、开发路线图

| 阶段 | 内容 | 预计工时 |
|------|------|---------|
| **Phase 1** | NAL-NL2 增益计算核心 + 单元测试 | 2 天 |
| **Phase 2** | 离线音频处理验证 (WAV 文件输入→处理→输出) | 1 天 |
| **Phase 3** | WASAPI Loopback 捕获 + 实时输出 | 1.5 天 |
| **Phase 4** | 实时 DSP 引擎 (STFT + WDRC) 集成 | 2 天 |
| **Phase 5** | GUI 界面 (听力图输入 + 增益曲线 + 控制面板) | 2 天 |
| **Phase 6** | 系统音量联动 (pycaw) + 延迟优化 + 打包 | 1.5 天 |

---

## 九、参考标准与资源

- **NAL-NL2 原始论文**: Keidser et al. (2011), *The NAL-NL2 Prescription Procedure*, Audiology Research
- **ISO 226:2003**: 标准等响曲线 (Equal Loudness Contours)
- **WASAPI Loopback**: [Microsoft Docs — Loopback Recording](https://docs.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording)
- **PortAudio / sounddevice**: [python-sounddevice.readthedocs.io](https://python-sounddevice.readthedocs.io/)
- **pycaw (音量控制)**: [Python Core Audio Windows](https://github.com/AndreMiras/pycaw)

---

> 📌 **下一步**：基于此方案，开始实现各个模块。是否开始编码？
