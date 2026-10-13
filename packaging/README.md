# Packaging

Installers for Linux (Flatpak), macOS (disk image) and Windows (setup program).
Each bundles Python, wxPython, the app's pinned dependencies and ffmpeg 8.1, so
users need nothing else. They are built by
[`.github/workflows/package.yml`](../.github/workflows/package.yml):

- **Push a tag** such as `v0.1.0`: every platform is built and a *draft*
  GitHub release is created with all installers and a `SHA256SUMS.txt`.
  Check it, then publish it.
- **Run the workflow by hand** (Actions › Package › Run workflow): the
  installers are kept as workflow artifacts.

Before tagging, set the version in `pyproject.toml` and add a `<release>` to
`flatpak/com.rationalboxes.ReVoiceOver.metainfo.xml`.

| Output | Built on | ffmpeg |
|---|---|---|
| `ReVoiceOver-<v>-linux-x86_64.flatpak`, `-aarch64` | Ubuntu runners, Freedesktop 25.08 runtime | built from source with libx264 |
| `ReVoiceOver-<v>-macos-arm64.dmg`, `-x86_64` | `macos-15`, `macos-15-intel` | built from source, Apple VideoToolbox H.264 |
| `ReVoiceOver-<v>-windows-x64-setup.exe` | `windows-2025` | BtbN's GPL build (n8.1) |

Every local build writes its installer to [`releases/`](../releases); the
intermediate app folders and caches go to `build/`.

## Files

```
requirements.txt          pinned app dependencies (all platforms)
requirements-bundle.txt   + wxPython and PyInstaller, for macOS and Windows
make_icons.py             app.svg → app.png / app.ico / app.icns
pyinstaller/              spec and entry points: ReVoiceOver (GUI) and narr (CLI)
flatpak/                  manifest, desktop entry, AppStream metadata, wheel lists
macos/                    build.sh (app + DMG), build_ffmpeg.sh, entitlements
windows/                  build.ps1, ReVoiceOver.iss (Inno Setup)
```

The bundled apps find their ffmpeg through `narration/media/ffmpeg.py`
(`ffmpeg/` inside the bundle, or `$NARR_FFMPEG_DIR`), before looking on `PATH`.

## Building locally

**Flatpak** (needs `flatpak-builder`; the first build compiles wxWidgets,
about 30 minutes):

```bash
packaging/flatpak/build.sh       # builds, installs for your user, writes releases/*.flatpak
flatpak run com.rationalboxes.ReVoiceOver
```

**macOS** (Python 3.13 on PATH, Xcode command line tools; on Intel also
`brew install nasm`):

```bash
packaging/macos/build.sh         # build/dist/ReVoiceOver.app and releases/*.dmg
```

**Windows** (Python 3.13 x64, [Inno Setup 6](https://jrsoftware.org/isinfo.php)):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1   # releases\*-setup.exe
```

## Updating dependencies

```bash
uv pip compile pyproject.toml --extra gui --universal --python-version 3.13 \
    --no-header -o packaging/requirements.txt
python3 packaging/flatpak/gen_python_deps.py    # refresh the Flatpak wheel lists
```

wxPython is pinned separately: in `requirements-bundle.txt` (macOS, Windows)
and as a source archive in the Flatpak manifest.

## Signing

The builds are unsigned. Users see a warning the first time they open the app.

- **macOS:** set `CODESIGN_IDENTITY` to a *Developer ID Application* identity
  before running `build.sh` to sign with the hardened runtime. Users get no
  warning only once the DMG is also notarized (`xcrun notarytool submit
  --wait`, then `xcrun stapler staple`). That needs an Apple Developer account.
- **Windows:** SmartScreen warns about unsigned installers until they gain a
  reputation. Sign `build\dist\ReVoiceOver\*.exe` and the setup program with
  `signtool` and a code-signing certificate.
