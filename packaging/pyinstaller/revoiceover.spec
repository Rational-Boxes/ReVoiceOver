# PyInstaller spec for the macOS app and the Windows program folder.
#
#   pyinstaller --noconfirm --distpath build/dist --workpath build/pyinstaller \
#       packaging/pyinstaller/revoiceover.spec
#
# Expects (made by the platform build scripts):
#   build/icons/app.icns, app.ico   packaging/make_icons.py
#   build/ffmpeg/ffmpeg[.exe], ffprobe[.exe]   bundled as ffmpeg/ (see media/ffmpeg.py)
# Produces build/dist/ReVoiceOver/ (both platforms) and build/dist/ReVoiceOver.app (macOS);
# the platform build scripts turn those into installers in releases/.
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
HERE = Path(SPECPATH).resolve()
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
MAC = sys.platform == "darwin"
WIN = sys.platform == "win32"

ffmpeg_dir = ROOT / "build" / "ffmpeg"
exe_suffix = ".exe" if WIN else ""
ffmpeg = [ffmpeg_dir / f"{name}{exe_suffix}" for name in ("ffmpeg", "ffprobe")]
missing = [str(p) for p in ffmpeg if not p.is_file()]
if missing:
    raise SystemExit(f"missing {', '.join(missing)}: run the platform build script first")

def analysis(script: str) -> Analysis:
    return Analysis(
        [str(HERE / script)],
        pathex=[str(ROOT)],
        binaries=[(str(p), "ffmpeg") for p in ffmpeg],
        datas=collect_data_files("narration"),  # icons, help pages, prompts
        # keyring picks its backend at runtime by entry point, invisible to analysis.
        hiddenimports=collect_submodules("keyring.backends") + collect_submodules("narration"),
        excludes=["tkinter", "pytest", "IPython"],
    )


a = analysis("revoiceover_gui.py")
c = analysis("narr_cli.py")
icon = str(ROOT / "build" / "icons" / ("app.icns" if MAC else "app.ico"))

gui = EXE(
    PYZ(a.pure), a.scripts, [], exclude_binaries=True,
    name="ReVoiceOver", console=False, icon=icon,
    # PyInstaller signs ad hoc on macOS; real signing happens after the build.
    codesign_identity=None, entitlements_file=None,
)
cli = EXE(
    PYZ(c.pure), c.scripts, [], exclude_binaries=True,
    name="narr", console=True, icon=icon,
)
coll = COLLECT(gui, a.binaries, a.datas, cli, c.binaries, c.datas, name="ReVoiceOver")

if MAC:
    app = BUNDLE(
        coll,
        name="ReVoiceOver.app",
        icon=icon,
        bundle_identifier="com.rationalboxes.ReVoiceOver",
        version=VERSION,
        info_plist={
            "CFBundleName": "ReVoiceOver",
            "CFBundleDisplayName": "ReVoiceOver",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.video",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "Copyright © Rational Boxes. GPL-3.0-or-later.",
        },
    )
