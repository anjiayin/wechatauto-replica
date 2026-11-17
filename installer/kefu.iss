; 微信客服系统 · Inno Setup 6 安装脚本
;
; 由 packaging/build_installer.py 调用 ISCC 编译，版本号/源目录/输出目录均通过
; /d 宏注入，脚本内不写死版本号。三条底线：
;   1. 装到 %LOCALAPPDATA%\WeChatKefu\，PrivilegesRequired=lowest（免 UAC）
;   2. 不打包 data\ 下任何业务数据（secret.key / license / sqlite 升级永不覆盖）
;   3. 不打包 app\current junction（由 launcher 首次启动时按 current.json 创建）

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SourcePath
  #define SourcePath ".."
#endif
#ifndef OutputDir
  #define OutputDir ".."
#endif

#define MyAppName "微信客服系统"
#define MyAppPublisher "微信客服"
#define MyAppExeName "start_kefu.bat"
#define MyAppId "{{B8A6F4C2-1D5E-4A3B-9C7F-6E2D4A8B1F30}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL=
DefaultDirName={localappdata}\WeChatKefu
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=WeChatKefu-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
AllowNoIcons=yes
DirExistsWarning=no
SetupIconFile={#SourcePath}\app.ico
UninstallDisplayIcon={app}\app.ico
UninstallDisplayName={#MyAppName} {#MyAppVersion}
; 无代码签名：安装时 SmartScreen/杀软可能提示，欢迎页下方有显式说明

[Languages]
; 中文语言文件随仓库携带（installer/Languages/），不依赖构建机 Inno Setup 安装目录
; 相对路径以 .iss 所在目录为基准；官方源文件见技术方案附录 C
Name: "chinese"; MessagesFile: "Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"; Flags: unchecked
Name: "startup"; Description: "开机自动启动（常驻托盘）"; GroupDescription: "启动选项:"; Flags: unchecked

[Files]
; 启动器和配置（bundle 根）
Source: "{#SourcePath}\launcher.pyc"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\updater.pyc"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\start_kefu.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\current.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\version.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\app.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourcePath}\delivery_seed.json"; DestDir: "{app}"; Flags: ignoreversion
; 版本层与稳定层（app\current junction 不在此列，由 launcher 重建）
Source: "{#SourcePath}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourcePath}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
; data 只放一个哨兵文件；绝不复制 sqlite / secret.key / 授权状态
Source: "{#SourcePath}\data\.gitkeep"; DestDir: "{app}\data"; Flags: onlyifdoesntexist uninsneveruninstall

[Registry]
; 开机自启写 HKCU（免管理员），卸载时移除该值
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "WeChatKefu"; ValueData: """{app}\{#MyAppExeName}"""; \
    Tasks: startup; Flags: uninsdeletevalue

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Description: "立即启动{#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 先删 A/B junction（只删链接，不删目标），否则卸载器可能跟着链接删文件
; 注意 PowerShell 的花括号须写成 {{ }} —— Inno 会把单花括号当常量起始符
Filename: "powershell"; Parameters: "-NoProfile -NonInteractive -Command ""$i=Get-Item -LiteralPath '{app}\app\current' -Force -ErrorAction SilentlyContinue; if($i){{$i.Delete()}}"""; Flags: runhidden

[UninstallDelete]
; 升级后新增的版本目录、下载缓存与日志是运行时生成的，卸载时一并清掉
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\data\update"
Type: files; Name: "{app}\data\kefu.log"
Type: files; Name: "{app}\data\kefu.log.1"
; data 里的 sqlite / secret.key / 授权状态刻意保留：重装不丢配置与授权

[Code]
// 首次安装前置确认页：无签名交付必须显式提醒关杀软（方案第 8 节）
var
  AntivirusPage: TWizardPage;
  AntivirusCheck: TNewCheckBox;

procedure AntivirusCheckClick(Sender: TObject);
begin
  WizardForm.NextButton.Enabled := AntivirusCheck.Checked;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  // 静默/无人值守安装（/VERYSILENT，企业批量部署）没有可点的下一步按钮，
  // 若此时禁用 NextButton 会导致 "Failed to proceed to next wizard page" 中止。
  // 静默模式 AntivirusPage 未创建（为 nil），也不能短路求值，故用显式分支。
  if WizardSilent then
  begin
    WizardForm.NextButton.Enabled := True;
    Exit;
  end;
  WizardForm.NextButton.Enabled :=
    (CurPageID <> AntivirusPage.ID) or AntivirusCheck.Checked;
end;

procedure InitializeWizard();
var
  Note: TNewStaticText;
begin
  // 静默安装不建自定义页，避免 CurPageChanged 引用未初始化对象
  if WizardSilent then Exit;

  AntivirusPage := CreateCustomPage(wpWelcome, '安装前须知', '关于安全软件');
  Note := TNewStaticText.Create(AntivirusPage);
  Note.Parent := AntivirusPage.Surface;
  Note.Left := 0;
  Note.Top := 0;
  Note.Width := AntivirusPage.SurfaceWidth;
  Note.WordWrap := True;
  Note.Caption :=
    '本程序需要读取微信进程数据、写入本机配置，且当前版本未做代码签名。' + #13#10 +
    '360 / 火绒 / Windows Defender 等安全软件可能拦截安装或首次启动。' + #13#10 +
    '请先关闭安全软件（或把安装目录加入白名单），程序安装并正常启动后再重新开启。';
  Note.Height := 80;

  AntivirusCheck := TNewCheckBox.Create(AntivirusPage);
  AntivirusCheck.Parent := AntivirusPage.Surface;
  AntivirusCheck.Left := 0;
  AntivirusCheck.Top := 96;
  AntivirusCheck.Width := AntivirusPage.SurfaceWidth;
  AntivirusCheck.Caption := '我已了解并已关闭安全软件（或已加入白名单）';
  AntivirusCheck.OnClick := @AntivirusCheckClick;
end;
