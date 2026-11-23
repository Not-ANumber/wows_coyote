; wows_coyote · Inno Setup 6+
; 先构建 dist\wows_coyote\wows_coyote.exe 和 releases\wows_coyote-<版本>-gpl.zip
; 再用 Inno Setup Compiler 编译本文件。
;
; 输出：dist_installer\wows_coyote-<版本>-setup.exe
;
; 版本号默认取本文件的值；tools/make_release.py 会通过
; /DMyAppVersion=<版本> 覆盖，无需手工改这里。

#define MyAppName "wows_coyote"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "wowsDG"
#define MyAppExeName "wows_coyote.exe"

[Setup]
AppId={{6F1C4C8D-2A1E-4B5A-9F0D-6C2B7A3E1D90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\wows_coyote
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist_installer
OutputBaseFilename=wows_coyote-{#MyAppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\assets\brand\wows-icon.ico
SetupLogging=yes
; 中文系统默认中文；双语言时允许在向导里切换。
ShowLanguageDialog=yes
LanguageDetectionMethod=uilanguage

[Languages]
; 随 installer 目录分发，不依赖 Inno Setup 是否自带中文。
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标"; Flags: unchecked

[InstallDelete]
; 旧版程序名为 "wows_coyote standalone"；升级时清理旧 exe 和快捷方式。
Type: files; Name: "{app}\wows_standalone.exe"
Type: files; Name: "{userprograms}\wows_coyote standalone\wows_coyote standalone.lnk"
Type: files; Name: "{userprograms}\wows_coyote standalone\卸载 wows_coyote standalone.lnk"
Type: dirifempty; Name: "{userprograms}\wows_coyote standalone"
Type: files; Name: "{userdesktop}\wows_coyote standalone.lnk"

[Files]
; 构建脚本已把 LICENSE/THIRD_PARTY/SOURCE_OFFER/CHANGELOG、source zip、
; 可选 wows_overlay.exe 放进 dist\wows_coyote，这里整体安装。
Source: "..\dist\wows_coyote\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\dist"
