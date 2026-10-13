; Inno Setup script for the Windows installer. Built by build.ps1, which passes
; /DAppVersion and has already made build\dist\ReVoiceOver (PyInstaller) and build\icons.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{C4766E43-37EF-49EB-AAD8-CF4DB29C9605}
AppName=ReVoiceOver
AppVersion={#AppVersion}
AppVerName=ReVoiceOver {#AppVersion}
AppPublisher=Rational Boxes
AppPublisherURL=https://github.com/Rational-Boxes/ReVoiceOver
AppSupportURL=https://github.com/Rational-Boxes/ReVoiceOver/issues
DefaultDirName={autopf}\ReVoiceOver
DefaultGroupName=ReVoiceOver
DisableProgramGroupPage=yes
; Installs for the current user without admin rights; the first page offers
; an all-users install instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=..\..\LICENSE
SetupIconFile=..\..\build\icons\app.ico
UninstallDisplayIcon={app}\ReVoiceOver.exe
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\..\releases
OutputBaseFilename=ReVoiceOver-{#AppVersion}-windows-x64-setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\build\dist\ReVoiceOver\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\build\ffmpeg\FFMPEG-LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\ReVoiceOver"; Filename: "{app}\ReVoiceOver.exe"
Name: "{autodesktop}\ReVoiceOver"; Filename: "{app}\ReVoiceOver.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ReVoiceOver.exe"; Description: "{cm:LaunchProgram,ReVoiceOver}"; Flags: nowait postinstall skipifsilent
