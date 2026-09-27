#define MyAppName "NEST"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "NEST"
[Setup]
AppId={{AB605617-0377-4DE2-A85A-EA05AF0DFA71}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\NEST
DefaultGroupName=NEST
PrivilegesRequired=lowest
OutputDir=release\installer
OutputBaseFilename=NEST_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\NEST.exe
[Files]
Source: "release\dist\NEST\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{group}\NEST"; Filename: "{app}\NEST.exe"
Name: "{autodesktop}\NEST"; Filename: "{app}\NEST.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked
[Run]
Filename: "{app}\NEST.exe"; Description: "Запустить NEST"; Flags: nowait postinstall skipifsilent
