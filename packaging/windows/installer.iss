; youtube-edit 윈도우 설치 프로그램 (Inno Setup 6.3 이상 — x64compatible, CreateDownloadPage)
;
; 빌드 (저장소 루트, PyInstaller 로 dist\youtube-edit\ 를 만든 뒤):
;   iscc /DAppVersion=0.1.0 packaging\windows\installer.iss
; 결과: dist\youtube-edit-<버전>-win-x64-setup.exe
;
; - 사용자별 설치: %LOCALAPPDATA%\Programs\youtube-edit (관리자 권한 불필요, PrivilegesRequired=lowest)
; - AppId 는 고정이다. 바꾸면 새 버전이 기존 설치를 덮어쓰지 못하고 따로 설치된다.
; - 같은 AppId 로 다시 설치하면 덮어쓴다. 이전 버전 라이브러리가 섞이지 않게 {app}\_internal 은 먼저 지운다.
; - WebView2 런타임이 없으면 Microsoft 의 Evergreen 부트스트래퍼(약 2MB)를 설치 중에 받아 실행한다.
;   부트스트래퍼는 어차피 런타임을 인터넷으로 받으므로 설치 파일에 넣지 않았다. URL 은 Microsoft 공식 고정 링크
;   (WebView2Url) 이고, 파일은 Microsoft 가 수시로 갱신·서명하므로 sha256 은 고정하지 않는다.
; - 사용자 데이터(%APPDATA%\youtube-edit: 모델·작업 캐시·설정)는 제거 때 지우지 않는다.

#define AppName "youtube-edit"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppExe "youtube-edit.exe"
#define RepoRoot "..\.."
#define DistDir RepoRoot + "\dist\youtube-edit"
#define WebView2Url "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
#define WebView2ClientKey "Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

[Setup]
AppId={{D0BC7895-A40A-4DA4-A625-AFAC09F4374D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=withbbang
AppPublisherURL=https://github.com/withbbang/video-edit
AppSupportURL=https://github.com/withbbang/video-edit/issues
AppUpdatesURL=https://github.com/withbbang/video-edit/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#RepoRoot}\LICENSE
OutputDir={#RepoRoot}\dist
OutputBaseFilename={#AppName}-{#AppVersion}-win-x64-setup
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "{#RepoRoot}\packaging\THIRD_PARTY.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  DownloadPage: TDownloadWizardPage;
  NeedWebView2: Boolean;

function HasWebView2Version(RootKey: Integer; SubKey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
end;

// https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution#detect-if-a-webview2-runtime-is-already-installed
function WebView2Installed: Boolean;
begin
  Result := HasWebView2Version(HKLM, 'SOFTWARE\WOW6432Node\{#WebView2ClientKey}')
    or HasWebView2Version(HKLM, 'SOFTWARE\{#WebView2ClientKey}')
    or HasWebView2Version(HKCU, 'Software\{#WebView2ClientKey}');
end;

procedure InitializeWizard;
begin
  NeedWebView2 := not WebView2Installed;
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and NeedWebView2 then begin
    DownloadPage.Clear;
    DownloadPage.Add('{#WebView2Url}', 'MicrosoftEdgeWebview2Setup.exe', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        NeedWebView2 := False;
        SuppressibleMsgBox('WebView2 런타임 설치 프로그램을 받지 못했습니다: ' + GetExceptionMessage + #13#10#13#10 +
          'youtube-edit 설치는 계속합니다. 앱 창이 열리지 않으면 아래에서 WebView2 런타임(Evergreen)을 설치해 주세요.' + #13#10 +
          'https://developer.microsoft.com/microsoft-edge/webview2/', mbInformation, MB_OK, IDOK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssPostInstall) and NeedWebView2 then begin
    WizardForm.StatusLabel.Caption := 'WebView2 런타임 설치 중…';
    if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '',
                SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      Log(Format('WebView2 부트스트래퍼 실패 (종료 코드 %d)', [ResultCode]));
    if not WebView2Installed then
      SuppressibleMsgBox('WebView2 런타임을 설치하지 못했습니다. 앱 창이 열리지 않으면 아래에서 설치해 주세요.' + #13#10 +
        'https://developer.microsoft.com/microsoft-edge/webview2/', mbInformation, MB_OK, IDOK);
  end;
end;
