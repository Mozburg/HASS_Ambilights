; HASS Ambilights installer (Inno Setup 6).
;
;   iscc /DAppVersion=2.15.0 packaging\hass-ambilights.iss
;
; Per-user, no admin prompt: the app is a tray program that only ever writes to
; %APPDATA%\HassAmbilights, so installing under %LOCALAPPDATA%\Programs keeps the
; program files and the user's settings on separate paths.
;
; Uninstalling is a clean reset: program files, settings, logs and the login entry.
;
; Kept deliberately plain - "x64" rather than the newer "x64compatible", and reg.exe
; rather than a [Registry] ValueType - so any Inno Setup 6.x on a build machine can
; compile it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "HASS Ambilights"
#define AppExe "HassAmbilights.exe"
#define AppPublisher "Mozburg"
#define AppUrl "https://github.com/Mozburg/HASS_Ambilights"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
; A stable AppId is what makes an upgrade an upgrade instead of a second install.
AppId={{7B2F4C6E-9A31-4D5E-8C7A-3F1E6D2B9C40}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\HASS Ambilights
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=HassAmbilights-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; The app is a tray program: Restart Manager closes it during an upgrade, and the
; /SILENT path the built-in updater uses depends on this being on.
CloseApplications=yes
CloseApplicationsFilter={#AppExe}
RestartApplications=no
MinVersion=10.0
AllowNoIcons=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.example.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Close the running app before the uninstaller deletes the exe.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden
; "Start with Windows" belongs to the app (HKCU Run, value HassAmbilights); remove it
; here so a clean reset cannot leave a login entry pointing at a missing exe.
Filename: "{sys}\reg.exe"; Parameters: "delete ""HKCU\{#RunKey}"" /v HassAmbilights /f"; Flags: runhidden

[UninstallDelete]
; The paths the app writes: app_data_dir() and log_file_path(). A CONFIG_FILE
; override is not covered, which is what the uninstall prompt says.
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{userappdata}\HassAmbilights"
Type: filesandordirs; Name: "{localappdata}\HassAmbilights"

[Code]
function InitializeUninstall(): Boolean;
begin
  Result := True;
  // Never block a silent uninstall: that path is the CI check and a scripted reset.
  if UninstallSilent() then
    Exit;
  Result := MsgBox(
    'Uninstalling HASS Ambilights also deletes everything it saved:' + #13#10 +
    #13#10 +
    '  ' + ExpandConstant('{userappdata}\HassAmbilights') + #13#10 +
    '      your Home Assistant address and access token,' + #13#10 +
    '      your calibration, zones, light map and every other setting' + #13#10 +
    '  ' + ExpandConstant('{localappdata}\HassAmbilights') + #13#10 +
    '      the logs' + #13#10 +
    #13#10 +
    'This is a clean reset and cannot be undone. Uninstall and delete all of it?',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;
