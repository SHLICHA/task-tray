; Установщик «Задачи» (Inno Setup 6). Собирается в GitHub Actions, см. .github/workflows/release.yml.
; Ставится без прав администратора в %LOCALAPPDATA%\Programs\Tasks.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8F3A2C51-6B7E-4D2A-9C1E-5A7B3D9E2F10}
AppName=Задачи
AppVersion={#AppVersion}
AppVerName=Задачи {#AppVersion}
AppPublisher=SHLICHA
AppPublisherURL=https://github.com/SHLICHA/task-tray
DefaultDirName={localappdata}\Programs\Tasks
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=TasksSetup
SetupIconFile=tasks.ico
UninstallDisplayIcon={app}\Tasks.exe
UninstallDisplayName=Задачи
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "autostart"; Description: "Запускать при входе в Windows"
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; Flags: unchecked

[InstallDelete]
; убрать файлы прошлой версии, чтобы не копились
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Tasks\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Задачи"; Filename: "{app}\Tasks.exe"
Name: "{autodesktop}\Задачи"; Filename: "{app}\Tasks.exe"; Tasks: desktopicon

[Registry]
; то же значение, что ставит галочка «Запускать при входе в систему» в программе
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "TaskTray"; ValueData: """{app}\Tasks.exe"" --hidden"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; тихое обновление из программы — сразу запустить новую версию
Filename: "{app}\Tasks.exe"; Parameters: "--after-update"; Flags: nowait; Check: WizardSilent
; обычная установка — галочка «Запустить» на последней странице
Filename: "{app}\Tasks.exe"; Description: "Запустить «Задачи»"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im Tasks.exe"; Flags: runhidden; RunOnceId: "StopTasks"

[Code]
function InitializeSetup(): Boolean;
begin
  { при обновлении из программы даём ей пару секунд закрыться }
  if WizardSilent() then
    Sleep(2500);
  Result := True;
end;
