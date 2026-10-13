#!/usr/bin/env bash
# Build static ffmpeg/ffprobe for the macOS app into build/ffmpeg/.
#
# Uses Apple's VideoToolbox (H.264) and AudioToolbox; no third-party libraries,
# so the binaries only link system frameworks and run on macOS 11+.
# x86_64 needs nasm (brew install nasm).
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION=8.1.3
SHA256=7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3
OUT=build/ffmpeg
SRC=build/ffmpeg-src

if [[ -x $OUT/ffmpeg && -x $OUT/ffprobe ]] && $OUT/ffmpeg -version | grep -q "version $VERSION"; then
    echo "ffmpeg $VERSION already built"; exit 0
fi
mkdir -p "$SRC" "$OUT"
tarball="$SRC/ffmpeg-$VERSION.tar.xz"
[[ -f $tarball ]] || curl -fsSL "https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz" -o "$tarball"
echo "$SHA256  $tarball" | shasum -a 256 -c -
tar -xf "$tarball" -C "$SRC"
cd "$SRC/ffmpeg-$VERSION"
export MACOSX_DEPLOYMENT_TARGET=11.0
./configure \
    --disable-shared --enable-static --disable-doc --disable-ffplay --disable-debug \
    --disable-autodetect --enable-videotoolbox --enable-audiotoolbox --enable-zlib \
    --extra-cflags="-mmacosx-version-min=11.0" --extra-ldflags="-mmacosx-version-min=11.0"
make -j"$(sysctl -n hw.ncpu)"
cp ffmpeg ffprobe "../../../$OUT/"
cd ../../..
otool -L "$OUT/ffmpeg" | grep -v -e /System/ -e /usr/lib/ -e ':$' && {
    echo "ffmpeg links non-system libraries" >&2; exit 1; } || true
"$OUT/ffmpeg" -hide_banner -encoders | grep -q h264_videotoolbox
echo "built $OUT/ffmpeg and ffprobe ($VERSION)"
