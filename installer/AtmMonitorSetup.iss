; Dashen ATM Monitor Setup — one signed EXE per release.
;
; The EXE carries the probe bundle, the pinned Zabbix Agent 2 MSI and the
; installer payload. The per-ATM profile is a separate file supplied by the
; portal; this wrapper never downloads anything.
;
; Build with installer/build_agent_setup.ps1, which sets PayloadDir and
; MyAppVersion. The signing step runs signtool after ISCC; see the README.

#define MyAppVersion GetEnv("ATM_SETUP_VERSION")
#if MyAppVersion == ""
  #define MyAppVersion "0.0.0-dev"
#endif

#define PayloadDir GetEnv("ATM_SETUP_PAYLOAD")
#if PayloadDir == ""
  #define PayloadDir "staging\payload"
#endif

#define MyAppName "Dashen ATM Monitor Setup"
#define MyAppPublisher "Dashen Bank"

[Setup]
AppId={{6E1B4B0E-6A1E-4F5B-9C2A-2D6B7E4D9A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={tmp}\AtmMonitorSetup
DefaultGroupName={#MyAppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=no
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=AtmMonitorSetup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
Uninstallable=no
CreateUninstallRegKey=no
SetupLogging=yes
UsePreviousAppDir=no
AllowNoIcons=yes

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Code]
var
  ProfilePath: String;
  AtmId: String;
  AcceptAtmId: String;
  ExpectedHash: String;
  IsDryRun: Boolean;

function GetArgValue(const Name: String; const Default: String): String;
var
  I: Integer;
  S, Prefix: String;
begin
  Result := Default;
  Prefix := '/' + Name + '=';
  for I := 1 to ParamCount do
  begin
    S := ParamStr(I);
    if CompareText(Copy(S, 1, Length(Prefix)), Prefix) = 0 then
    begin
      Result := Copy(S, Length(Prefix) + 1, Length(S));
      Exit;
    end;
  end;
end;

function HasFlag(const Name: String): Boolean;
var
  I: Integer;
  S, Flag: String;
begin
  Result := False;
  Flag := '/' + Name;
  for I := 1 to ParamCount do
  begin
    S := ParamStr(I);
    if CompareText(S, Flag) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function FindOnlyProfile(const Directory: String): String;
var
  FindRec: TFindRec;
  Count: Integer;
  Found: String;
begin
  Result := '';
  Count := 0;
  Found := '';
  if not FindFirst(AddBackslash(Directory) + '*.profile.json', FindRec) then
  begin
    MsgBox('No .profile.json was found next to the installer.' + #13#10 +
           'Download the profile from the portal and put it beside this EXE,' + #13#10 +
           'or pass /PROFILE=<path>.', mbError, MB_OK);
    Exit;
  end;
  try
    repeat
      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
      begin
        Count := Count + 1;
        Found := AddBackslash(Directory) + FindRec.Name;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
  if Count = 1 then
    Result := Found
  else if Count > 1 then
    MsgBox('More than one .profile.json was found next to the installer.' + #13#10 +
           'Pass /PROFILE=<path> so the right ATM is chosen.', mbError, MB_OK)
  else
    MsgBox('No .profile.json was found next to the installer.' + #13#10 +
           'Download the profile from the portal and put it beside this EXE,' + #13#10 +
           'or pass /PROFILE=<path>.', mbError, MB_OK);
end;

function ProfileAtmId(const Path: String): String;
var
  Name: String;
begin
  Name := ExtractFileName(Path);
  if CompareText(Copy(Name, Length(Name) - Length('.profile.json') + 1,
                      Length('.profile.json')), '.profile.json') = 0 then
    Name := Copy(Name, 1, Length(Name) - Length('.profile.json'));
  Result := Name;
end;

function InitializeSetup(): Boolean;
var
  Arg: String;
begin
  Result := True;
  IsDryRun := HasFlag('DRYRUN');
  AcceptAtmId := GetArgValue('ACCEPTATM', '');
  ExpectedHash := GetArgValue('EXPECTEDHASH', '');

  Arg := GetArgValue('PROFILE', '');
  if Arg <> '' then
    ProfilePath := ExpandConstant(Arg)
  else
    ProfilePath := FindOnlyProfile(ExtractFileDir(ExpandConstant('{srcexe}')));

  if ProfilePath = '' then
  begin
    Result := False;
    Exit;
  end;
  if not FileExists(ProfilePath) then
  begin
    MsgBox('Profile not found: ' + ProfilePath, mbError, MB_OK);
    Result := False;
    Exit;
  end;

  AtmId := ProfileAtmId(ProfilePath);
  if IsDryRun then
  begin
    if MsgBox('DRY RUN — no changes will be made.' + #13#10#13#10 +
              'Profile: ' + ProfilePath + #13#10 +
              'ATM: ' + AtmId + #13#10#13#10 +
              'Continue?', mbConfirmation, MB_YESNO) <> IDYES then
      Result := False;
    Exit;
  end;

  if MsgBox('Apply this agent profile to this ATM?' + #13#10#13#10 +
            'Profile: ' + ProfilePath + #13#10 +
            'ATM: ' + AtmId + #13#10#13#10 +
            'The installer will write C:\monitor, install or update' + #13#10 +
            'Zabbix Agent 2 6.4, and restart the agent service.' + #13#10 +
            'Files that change are backed up under C:\monitor\backup.' + #13#10#13#10 +
            'Confirm the ATM id matches the machine you are on.',
            mbConfirmation, MB_YESNO) <> IDYES then
    Result := False;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Params, LogPath: String;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  LogPath := ExpandConstant('{commonappdata}\AtmMonitor\logs\install.log');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
            ExpandConstant('{app}\atm-install.ps1') + '"' +
            ' -Profile "' + ProfilePath + '"' +
            ' -MonitorRoot "C:\monitor"' +
            ' -LogPath "' + LogPath + '"';

  if IsDryRun then
    Params := Params + ' -DryRun';
  if HasFlag('ALLOWNOTREADY') then
    Params := Params + ' -AllowNotReady';
  if HasFlag('ALLOWUNSIGNED') then
    Params := Params + ' -AllowUnsignedProfile';
  if HasFlag('REPLACELEGACY') then
    Params := Params + ' -ReplaceLegacyAgent';
  if HasFlag('REASSIGN') then
    Params := Params + ' -Reassign';
  if HasFlag('NOAGENTINSTALL') then
    Params := Params + ' -NoAgentInstall';
  if HasFlag('NOSERVICE') then
    Params := Params + ' -NoService';
  if HasFlag('NOSERVICEACCOUNTTEST') then
    Params := Params + ' -NoServiceAccountTest';
  if HasFlag('ALLOWUNVERIFIED') then
    Params := Params + ' -AllowUnverifiedServiceAccount';
  if AcceptAtmId <> '' then
    Params := Params + ' -AcceptAtmId "' + AcceptAtmId + '"';
  if ExpectedHash <> '' then
    Params := Params + ' -ExpectedProfileSha256 "' + ExpectedHash + '"';

  if not Exec('powershell.exe', Params, '', SW_SHOW, ewWaitUntilTerminated,
              ResultCode) then
  begin
    MsgBox('Could not start the installer payload.' + #13#10 +
           'See ' + LogPath, mbError, MB_OK);
    Abort;
  end;

  if ResultCode <> 0 then
  begin
    MsgBox('The installer stopped with exit code ' + IntToStr(ResultCode) + '.' + #13#10#13#10 +
           'A backup of any file it replaced is under C:\monitor\backup.' + #13#10 +
           'The log is at ' + LogPath + '.' + #13#10#13#10 +
           'Nothing was registered in Zabbix; fix the reported cause and run it again.',
           mbError, MB_OK);
    Abort;
  end;

  if IsDryRun then
    MsgBox('Dry run complete. No changes were made.', mbInformation, MB_OK)
  else
    MsgBox('Agent setup complete.' + #13#10#13#10 +
           'Profile: ' + ProfilePath + #13#10 +
           'ATM: ' + AtmId + #13#10#13#10 +
           'The agent is passive-only. Confirm the proxy can reach TCP 10050' + #13#10 +
           'and that the host shows data in Zabbix.', mbInformation, MB_OK);
end;
