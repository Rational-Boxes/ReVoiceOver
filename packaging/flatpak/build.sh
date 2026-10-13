#!/usr/bin/env bash
# Build the Flatpak, install it for the current user, and write a single-file
# bundle to releases/ReVoiceOver-<version>-linux-<arch>.flatpak.
#
#   packaging/flatpak/build.sh            build, install, bundle
#   packaging/flatpak/build.sh --no-install
set -euo pipefail
cd "$(dirname "$0")/../.."
APP=com.rationalboxes.ReVoiceOver
VERSION=$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
ARCH=$(flatpak --default-arch)
install=(--install)
[[ "${1:-}" == --no-install ]] && install=()

flatpak --user remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak-builder --user "${install[@]}" --force-clean --install-deps-from=flathub \
    --state-dir=build/flatpak-state --repo=build/flatpak-repo \
    build/flatpak-app "packaging/flatpak/$APP.yml"
mkdir -p releases
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    build/flatpak-repo "releases/ReVoiceOver-$VERSION-linux-$ARCH.flatpak" "$APP"
echo "wrote releases/ReVoiceOver-$VERSION-linux-$ARCH.flatpak"
