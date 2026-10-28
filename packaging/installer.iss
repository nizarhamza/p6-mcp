; P6 MCP installer (Inno Setup 6). Per-user install, no administrator rights.
;   ISCC packaging\installer.iss /DAppVersion=3.0.0
; Signing (optional): ISCC ... /DSIGN "/Sp6sign=signtool sign /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 /f cert.pfx /p PASSWORD $f"
; Expects packaging\dist\P6MCP (PyInstaller) and the brand files in assets\brand.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef RepoUrl
  #define RepoUrl "https://github.com/nizarhamza/p6-mcp"
#endif

[Setup]
AppId={{6F3A2D5E-9B1C-4C8E-A7D2-2E5B8F0C4A91}
AppName=P6 MCP for Primavera P6
AppVersion={#AppVersion}
AppVerName=P6 MCP {#AppVersion}
AppPublisher=Nizar Hamza
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
AppComments=Local MCP server for Primavera P6 Professional databases and XER files. Not affiliated with Oracle.
DefaultDirName={localappdata}\Programs\P6 MCP
DefaultGroupName=P6 MCP
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=dist
OutputBaseFilename=P6MCP-Setup-{#AppVersion}
SetupIconFile=..\assets\brand\ico\p6mcp.ico
UninstallDisplayIcon={app}\p6mcp.ico
WizardSmallImageFile=..\assets\brand\png\p6mcp-icon-256.png
UninstallDisplayName=P6 MCP for Primavera P6
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
AppMutex=P6MCP-Tray
; held while setup runs: both programs exit at once when they see it (runtime.setup_running), so a start
; during an update cannot lock a file setup is replacing
SetupMutex=P6MCP-Setup
CloseApplications=yes
RestartApplications=no
#ifexist "..\LICENSE"
LicenseFile=..\LICENSE
#endif
#ifdef SIGN
SignTool=p6sign
SignedUninstaller=yes
#endif

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[CustomMessages]
en.RegisterWith=Register P6 MCP with %1 (restart it afterwards)
fr.RegisterWith=Enregistrer P6 MCP dans %1 (relancez-le ensuite)
en.RunAtStartup=Start P6 MCP when I sign in to Windows (notification area)
fr.RunAtStartup=Lancer P6 MCP à l'ouverture de ma session Windows (zone de notification)
en.OpenSettings=Open P6 MCP Settings to choose a database
fr.OpenSettings=Ouvrir les paramètres P6 MCP pour choisir une base
en.Privacy=P6 MCP runs only on this PC. The data its tools return goes to the AI model your MCP client uses: for confidential schedules, use a local model.
fr.Privacy=P6 MCP fonctionne uniquement sur ce PC. Les données renvoyées par ses outils partent vers le modèle d'IA de votre client MCP : pour des plannings confidentiels, utilisez un modèle local.

[Tasks]
Name: "startup"; Description: "{cm:RunAtStartup}"; Flags: unchecked

[Files]
Source: "dist\P6MCP\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\NOTICE"; DestDir: "{app}"; DestName: "NOTICE.txt"; Flags: ignoreversion
Source: "..\assets\brand\ico\p6mcp.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\P6 MCP Settings"; Filename: "{app}\p6mcp-settings.exe"; IconFilename: "{app}\p6mcp.ico"
Name: "{group}\{cm:UninstallProgram,P6 MCP}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\p6mcp-server.exe"; Parameters: "setup startup on"; Flags: runhidden; Tasks: startup
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients register claude_desktop"; Description: "{cm:RegisterWith,Claude Desktop}"; Flags: postinstall runhidden; Check: ClientDetected('claude_desktop') and not IsAutoUpdate
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients register claude_code"; Description: "{cm:RegisterWith,Claude Code}"; Flags: postinstall runhidden; Check: ClientDetected('claude_code') and not IsAutoUpdate
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients register cursor"; Description: "{cm:RegisterWith,Cursor}"; Flags: postinstall runhidden; Check: ClientDetected('cursor') and not IsAutoUpdate
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients register vscode"; Description: "{cm:RegisterWith,VS Code}"; Flags: postinstall runhidden; Check: ClientDetected('vscode') and not IsAutoUpdate
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients register vscode_insiders"; Description: "{cm:RegisterWith,VS Code Insiders}"; Flags: postinstall runhidden; Check: ClientDetected('vscode_insiders') and not IsAutoUpdate
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients register lmstudio"; Description: "{cm:RegisterWith,LM Studio}"; Flags: postinstall runhidden; Check: ClientDetected('lmstudio') and not IsAutoUpdate
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients register cline"; Description: "{cm:RegisterWith,Cline}"; Flags: postinstall runhidden; Check: ClientDetected('cline') and not IsAutoUpdate
Filename: "{app}\p6mcp-settings.exe"; Description: "{cm:OpenSettings}"; Flags: postinstall nowait skipifsilent
; an automatic update (/AUTOUPDATE=1, run by the tray app after it quit): start the tray app again
Filename: "{app}\p6mcp-settings.exe"; Parameters: "--minimized"; Flags: nowait; Check: IsAutoUpdate

[UninstallRun]
; remove only the p6mcp entries, the P6 skills (and command files of 1.7 and earlier) P6 MCP wrote, and the startup entry; settings, backups and logs are kept
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients unregister --detected"; Flags: runhidden; RunOnceId: "UnregisterClients"
Filename: "{app}\p6mcp-server.exe"; Parameters: "clients skills remove --detected"; Flags: runhidden; RunOnceId: "RemoveSkills"
Filename: "{app}\p6mcp-server.exe"; Parameters: "setup startup off"; Flags: runhidden; RunOnceId: "StartupOff"

[Code]
var
  Detected: TStringList;

function IsAutoUpdate: Boolean;
begin
  { the tray app's silent update: client registrations are left as the user set them }
  Result := ExpandConstant('{param:AUTOUPDATE|0}') = '1';
end;

function ClientDetected(Id: String): Boolean;
var
  Code: Integer;
  ListFile: String;
begin
  if Detected = nil then
  begin
    Detected := TStringList.Create;
    ListFile := ExpandConstant('{tmp}\p6mcp-clients.txt');
    if Exec(ExpandConstant('{cmd}'), '/C ""' + ExpandConstant('{app}\p6mcp-server.exe') +
            '" setup detected-clients > "' + ListFile + '""', '', SW_HIDE, ewWaitUntilTerminated, Code)
       and (Code = 0) and FileExists(ListFile) then
      Detected.LoadFromFile(ListFile);
  end;
  Result := Detected.IndexOf(Id) >= 0;
end;

procedure InitializeWizard;
begin
  WizardForm.WelcomeLabel2.Caption := WizardForm.WelcomeLabel2.Caption + #13#10#13#10 + CustomMessage('Privacy');
end;
