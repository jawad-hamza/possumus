; Inno Setup 6 script for the ZehnMS desktop build (one shop, one PC).
;
;   ISCC.exe /DAppVersion=1.0.0 desktop\installer.iss
;
; Input : desktop\dist\ZehnMS\        (made by PyInstaller, see build.ps1)
; Output: desktop\dist\installer\ZehnMS-Setup-<version>.exe
;
; Per-user install: no administrator rights, program under
;   %LOCALAPPDATA%\Programs\ZehnMS
; Shop data (database, uploads, logs, backups, secret key) is written by the app to
;   %LOCALAPPDATA%\ZehnMS
; and is deliberately NOT removed on uninstall or upgrade.
;
; ZehnMS opens in its own window on the Microsoft Edge WebView2 runtime. Windows 11 and nearly
; every Windows 10 PC already have it. When it is missing, setup installs it if the small
; Microsoft bootstrapper was placed at desktop\redist\MicrosoftEdgeWebview2Setup.exe before
; building (see build.ps1 -FetchWebView2); otherwise it says so and ZehnMS falls back to a
; Microsoft Edge app window, which needs nothing extra.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "ZehnMS"
#define AppPublisher "ZehnMS"
#define AppExeName "ZehnMS.exe"
; Must match APP_USER_MODEL_ID in launcher.py so a pinned shortcut and the running window share one taskbar button.
#define AppUserModelId "ZehnMS.Desktop"
#define WebView2Bootstrapper "MicrosoftEdgeWebview2Setup.exe"

#if FileExists(AddBackslash(SourcePath) + "redist\" + WebView2Bootstrapper)
  #define BundleWebView2
#endif
#if FileExists(AddBackslash(SourcePath) + "zehnms.ico")
  #define HaveIcon
#endif

[Setup]
AppId={{BDBECAEF-81DE-45BC-AAB7-E132418B31FA}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist\installer
OutputBaseFilename=ZehnMS-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
#ifdef HaveIcon
SetupIconFile=zehnms.ico
#endif
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\ZehnMS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef BundleWebView2
Source: "redist\{#WebView2Bootstrapper}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: WebView2Missing
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelId}"; Comment: "Open the ZehnMS point of sale"
; Closing the window already stops ZehnMS. This shortcut is for a copy that serves other tills without a window.
Name: "{group}\Stop {#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--stop"; IconFilename: "{app}\{#AppExeName}"; Comment: "Shut down ZehnMS on this PC"
; Forgot-password recovery for an offline shop: prints a temporary owner password and keeps the window open.
Name: "{group}\Reset Owner Password"; Filename: "{cmd}"; Parameters: "/k """"{app}\zehnms-cli.exe"" --manage reset_owner_password"""; WorkingDir: "{app}"; Comment: "Create a temporary password for the owner account"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon

[Run]
#ifdef BundleWebView2
; Not elevated, so the runtime is installed for this Windows user only. Needs internet once.
Filename: "{tmp}\{#WebView2Bootstrapper}"; Parameters: "/silent /install"; StatusMsg: "Installing the Microsoft Edge WebView2 runtime..."; Check: WebView2Missing; Flags: waituntilterminated
#endif
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExeName}"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopZehnMSServer"

[UninstallDelete]
; Only files the program itself created inside the install folder (bytecode caches).
; %LOCALAPPDATA%\ZehnMS is intentionally absent from this section.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
const
  WebView2ClientKey = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

// The registry values Microsoft documents for detecting the Evergreen WebView2 runtime
// (launcher.py, webview2_runtime_version, reads the same ones).
function WebView2VersionIn(RootKey: Integer): String;
var
  Version: String;
begin
  Result := '';
  if RegQueryStringValue(RootKey, WebView2ClientKey, 'pv', Version) then
    if (Version <> '') and (Version <> '0.0.0.0') then
      Result := Version;
end;

function WebView2Missing: Boolean;
begin
  // HKLM32 is HKLM\SOFTWARE\WOW6432Node on 64-bit Windows, where a machine-wide runtime registers.
  Result := (WebView2VersionIn(HKLM32) = '') and (WebView2VersionIn(HKLM64) = '') and (WebView2VersionIn(HKCU) = '');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WebView2Missing and not WizardSilent then
    MsgBox('ZehnMS opens in its own window using the Microsoft Edge WebView2 runtime, which was not found on this PC'
#ifdef BundleWebView2
           + ' and could not be installed just now (it is downloaded from Microsoft, so it needs internet once).'
#else
           + '.'
#endif
           + #13#10 + #13#10 +
           'ZehnMS still works: it will open in a Microsoft Edge app window instead.' + #13#10 + #13#10 +
           'To get the standard window later, install the free "Evergreen Bootstrapper" from' + #13#10 +
           'https://developer.microsoft.com/microsoft-edge/webview2/ and start ZehnMS again.',
           mbInformation, MB_OK);
end;

// An upgrade cannot replace files that the running server holds open, so stop it first.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExistingExe: String;
  ResultCode: Integer;
begin
  Result := '';
  ExistingExe := ExpandConstant('{app}\{#AppExeName}');
  if FileExists(ExistingExe) then
    Exec(ExistingExe, '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('ZehnMS was removed.' + #13#10 + #13#10 +
           'Your shop data was kept in:' + #13#10 +
           ExpandConstant('{localappdata}\ZehnMS') + #13#10 + #13#10 +
           'Reinstalling ZehnMS picks it up again. Delete that folder yourself only if you no longer need the data.',
           mbInformation, MB_OK);
end;
