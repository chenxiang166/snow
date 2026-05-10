; Inno Setup 脚本 — PC 音频补偿软件安装包
; 包含 VB-Cable 驱动 + 主程序

[Setup]
AppName=PC 音频补偿
AppVersion=1.0
DefaultDirName={autopf}\PC-Audio-Comp
DefaultGroupName=PC 音频补偿
OutputDir=.\installer
OutputBaseFilename=PC-Audio-Comp-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=PC 音频补偿
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
SetupLogging=yes

[Files]
; 主程序
Source: "dist\PC-Audio-Comp.exe"; DestDir: "{app}"; Flags: ignoreversion

; VB-Cable 驱动 (所有文件)
Source: "driver\*.inf"; DestDir: "{tmp}\vbcable"; Flags: deleteafterinstall
Source: "driver\*.sys"; DestDir: "{tmp}\vbcable"; Flags: deleteafterinstall
Source: "driver\*.cat"; DestDir: "{tmp}\vbcable"; Flags: deleteafterinstall
Source: "driver\VBCABLE_Setup_x64.exe"; DestDir: "{tmp}\vbcable"; Flags: deleteafterinstall
Source: "driver\VBCABLE_Setup.exe"; DestDir: "{tmp}\vbcable"; Flags: deleteafterinstall

[Icons]
Name: "{group}\PC 音频补偿"; Filename: "{app}\PC-Audio-Comp.exe"
Name: "{commondesktop}\PC 音频补偿"; Filename: "{app}\PC-Audio-Comp.exe"

[Run]
; 安装 VB-Cable 驱动
Filename: "{tmp}\vbcable\VBCABLE_Setup_x64.exe"; StatusMsg: "正在安装虚拟音频驱动..."; Flags: waituntilterminated; Check: Is64BitInstallMode
Filename: "{tmp}\vbcable\VBCABLE_Setup.exe"; StatusMsg: "正在安装虚拟音频驱动..."; Flags: waituntilterminated; Check: not Is64BitInstallMode

; 启动程序。VB-Cable 首次安装后通常需要重启，默认不勾选，避免设备尚未枚举就启动失败。
Filename: "{app}\PC-Audio-Comp.exe"; Description: "启动 PC 音频补偿（若刚安装驱动，请先重启 Windows）"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
; 卸载时移除 VB-Cable (需设备管理器手动操作, 这里只提示)
Filename: "cmd"; Parameters: "/c echo 请手动在设备管理器中卸载 VB-Cable 驱动"; Flags: runhidden
