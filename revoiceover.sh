#!/usr/bin/env bash
# Start the ReVoiceOver desktop app.
#
#   ./revoiceover.sh                 open the app
#   ./revoiceover.sh VIDEO.mp4       start a new project for a video
#   ./revoiceover.sh WORK_FOLDER     open an existing project
#   ./revoiceover.sh --env-file F    read the DeepInfra token from F
#
# Works from any directory (and through a symlink, e.g. in ~/.local/bin).
# On first run it creates .venv next to this script and installs the app.
set -euo pipefail

# Resolve the real location of this script, following symlinks.
src="${BASH_SOURCE[0]}"
while [ -L "$src" ]; do
    dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
done
ROOT="$(cd -P "$(dirname "$src")" && pwd)"
VENV="$ROOT/.venv"
PY=/usr/bin/python3   # the system Python, which has the distribution's wxPython

die() { printf 'revoiceover: %s\n' "$*" >&2; exit 1; }

# --- prerequisites ---------------------------------------------------------
"$PY" -c 'import wx' 2>/dev/null ||
    die "wxPython is missing. On Fedora: sudo dnf install python3-wxpython4 python3-wxpython4-media"
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null ||
    die "ffmpeg/ffprobe not found. On Fedora: sudo dnf install ffmpeg-free"

# --- virtual environment (first run, or after the venv was removed) --------
if [ ! -x "$VENV/bin/narr-gui" ]; then
    echo "revoiceover: setting up $VENV (first run)…" >&2
    if command -v uv >/dev/null; then
        uv venv --quiet --python "$PY" --system-site-packages "$VENV"
        uv pip install --quiet --python "$VENV/bin/python" -e "$ROOT[gui]"
    else
        "$PY" -m venv --system-site-packages "$VENV"
        "$VENV/bin/pip" install --quiet -e "$ROOT[gui]"
    fi
fi

# --- token ----------------------------------------------------------------
# When launched from a desktop menu the shell environment (and the repo's .env)
# isn't visible, so point the app at the repo's .env unless told otherwise.
args=("$@")
if [ -z "${DEEPINFRA_TOKEN:-}${DEEPINFRA_KEY:-}" ] && [ -f "$ROOT/.env" ] &&
   [[ " $* " != *" --env-file "* ]]; then
    args=(--env-file "$ROOT/.env" "${args[@]}")
fi

exec "$VENV/bin/narr-gui" "${args[@]}"
