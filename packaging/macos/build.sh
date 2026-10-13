#!/usr/bin/env bash
# Build ReVoiceOver.app and a drag-to-install disk image:
#   releases/ReVoiceOver-<version>-macos-<arch>.dmg
#
# Run on the architecture you are building for (arm64 or x86_64), with
# Python 3.13 on PATH. Set CODESIGN_IDENTITY to sign with a Developer ID
# (otherwise the app is signed ad hoc and Gatekeeper asks before first launch).
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
ARCH=$(uname -m)

python3 -m venv build/venv
build/venv/bin/pip install --quiet --upgrade pip
build/venv/bin/pip install --quiet -r packaging/requirements-bundle.txt
build/venv/bin/pip install --quiet --no-deps .

packaging/macos/build_ffmpeg.sh
build/venv/bin/python packaging/make_icons.py build/icons
build/venv/bin/pyinstaller --noconfirm --clean --distpath build/dist --workpath build/pyinstaller \
    packaging/pyinstaller/revoiceover.spec

APP=build/dist/ReVoiceOver.app
if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
    codesign --force --deep --options runtime --timestamp \
        --entitlements packaging/macos/entitlements.plist --sign "$CODESIGN_IDENTITY" "$APP"
fi
codesign --verify --deep --strict "$APP"

# The disk image: the app plus an Applications link to drag it onto.
stage=build/dmg
rm -rf "$stage" && mkdir -p "$stage"
cp -R "$APP" "$stage/"
ln -s /Applications "$stage/Applications"
mkdir -p releases
DMG="releases/ReVoiceOver-$VERSION-macos-$ARCH.dmg"
rm -f "$DMG"
hdiutil create -volname "ReVoiceOver $VERSION" -srcfolder "$stage" -fs HFS+ \
    -format UDZO -imagekey zlib-level=9 "$DMG"
echo "wrote $DMG"
