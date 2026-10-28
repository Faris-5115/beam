; Beam-Setup.exe (Beam, a fork of MultiDrop): per-user install, no admin rights (Inno Setup 6).
;   iscc /DAppVersion=0.2.1 packaging\windows\beam.iss   (after building dist\Beam.exe
;   and build\mirror, the screen mirroring engine: packaging\windows\make_mirror_pack.py)
; The "Send to" shortcut and the Run value have the same names as in
; src/multidrop/winshell.py, so the tray menu and the installer switch the same entries.
; The firewall rule is per machine: the "firewall" task asks Windows once for admin
; rights (Beam.exe --allow-network); everything else stays per user.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"
#define FirewallRule "Beam (local network)"

[Setup]
AppId={{3C31AF79-644F-494D-A516-7C91D3C3DBE9}
AppName=Beam
AppVersion={#AppVersion}
AppPublisher=Beam
AppPublisherURL=https://github.com/Faris-5115/beam
DefaultDirName={localappdata}\Programs\Beam
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
SetupIconFile=..\..\data\icons\beam.ico
UninstallDisplayIcon={app}\Beam.exe
OutputDir=..\..\dist
OutputBaseFilename=Beam-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The tray app has no window: close it for updates and uninstalling.
CloseApplications=force
RestartApplications=no

[Tasks]
Name: sendto; Description: "Show Beam in Explorer's ""Send to"" menu"
Name: autostart; Description: "Start Beam with Windows"
Name: firewall; Description: "Let phones on my Wi-Fi reach Beam (Windows asks for permission once)"
Name: desktopicon; Description: "Desktop shortcut"; Flags: unchecked

[Files]
Source: "..\..\dist\Beam.exe"; DestDir: "{app}"; Flags: ignoreversion
; AirPlay screen mirroring: doubletake and its private GStreamer (win_mirror.py).
Source: "..\..\build\mirror\*"; DestDir: "{app}\mirror"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{userprograms}\Beam"; Filename: "{app}\Beam.exe"
Name: "{usersendto}\Beam"; Filename: "{app}\Beam.exe"; Tasks: sendto
Name: "{userdesktop}\Beam"; Filename: "{app}\Beam.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "{#RunKey}"; ValueType: string; ValueName: "Beam"; \
  ValueData: """{app}\Beam.exe"" --no-browser"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\Beam.exe"; Description: "Start Beam"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Also switched on later from the tray menu.
Type: files; Name: "{usersendto}\Beam.lnk"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  { ShellExec instead of [Run]: saying "No" to the admin prompt is no error,
    Beam then offers it again in its window. }
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('firewall') then
    ShellExec('runas', ExpandConstant('{app}\Beam.exe'), '--allow-network', '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
    { Added by the installer or by Beam itself; needs admin rights to go. }
    ShellExec('runas', ExpandConstant('{sys}\netsh.exe'),
              'advfirewall firewall delete rule name="{#FirewallRule}"', '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode);
  { The tray menu may have added it without the installer task. }
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, '{#RunKey}', 'Beam');
end;
