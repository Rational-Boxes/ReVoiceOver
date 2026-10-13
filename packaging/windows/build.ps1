# Build the Windows installer: releases\ReVoiceOver-<version>-windows-x64-setup.exe
#
# Needs Python 3.13 (x64) and Inno Setup 6 (iscc.exe on PATH, or in its
# default install folder). Run from any directory:
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is slow with the bar
Set-Location (Resolve-Path "$PSScriptRoot\..\..")

function Check { if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE)" } }

$Version = python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"; Check

python -m venv build\venv; Check
$Py = "build\venv\Scripts\python.exe"
& $Py -m pip install --quiet --upgrade pip; Check
& $Py -m pip install --quiet -r packaging\requirements-bundle.txt; Check
& $Py -m pip install --quiet --no-deps .; Check

# ffmpeg: BtbN's GPL build of the same release line as the other platforms.
$FfmpegZip = "build\ffmpeg-win64.zip"
if (-not (Test-Path "build\ffmpeg\ffmpeg.exe")) {
    $Url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip"
    Invoke-WebRequest $Url -OutFile $FfmpegZip
    Expand-Archive $FfmpegZip -DestinationPath build\ffmpeg-unpacked -Force
    New-Item -ItemType Directory -Force build\ffmpeg | Out-Null
    Get-ChildItem build\ffmpeg-unpacked -Recurse -Include ffmpeg.exe, ffprobe.exe |
        Copy-Item -Destination build\ffmpeg
    # The licence of the bundled build travels with it.
    Get-ChildItem build\ffmpeg-unpacked -Recurse -Filter LICENSE.txt | Select-Object -First 1 |
        Copy-Item -Destination build\ffmpeg\FFMPEG-LICENSE.txt
}
& build\ffmpeg\ffmpeg.exe -hide_banner -version | Select-Object -First 1

& $Py packaging\make_icons.py build\icons; Check
& build\venv\Scripts\pyinstaller.exe --noconfirm --clean --distpath build\dist --workpath build\pyinstaller `
    packaging\pyinstaller\revoiceover.spec; Check
& build\dist\ReVoiceOver\narr.exe --help | Out-Null; Check   # the bundle starts

$Iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $Iscc) { $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
& $Iscc /Qp "/DAppVersion=$Version" packaging\windows\ReVoiceOver.iss; Check
Write-Host "wrote releases\ReVoiceOver-$Version-windows-x64-setup.exe"
