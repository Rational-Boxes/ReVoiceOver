#!/usr/bin/env python3
"""Write python3-deps.json: the app's pinned wheels as Flatpak sources.

Flatpak builds run offline, so every wheel is listed with its URL and sha256,
for x86_64 and aarch64 (pure-Python wheels serve both). Run it again after
changing packaging/requirements.txt:

    python3 packaging/flatpak/gen_python_deps.py

Also writes the build-only tools wxPython needs to build from source
(python3-wx-build-deps.json).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

HERE = Path(__file__).resolve().parent
REQUIREMENTS = HERE.parent / "requirements.txt"
PYTHON = "313"  # the Freedesktop 25.08 runtime's Python
ARCHES = {"x86_64": "x86_64", "aarch64": "aarch64"}
ENV = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
       "implementation_name": "cpython", "platform_python_implementation": "CPython",
       "python_version": "3.13", "python_full_version": "3.13.0"}
# wxPython's build requirements (from its pyproject.toml) and their dependencies.
WX_BUILD = ["setuptools==80.9.0", "cython==3.1.4", "sip==6.12.0", "packaging==25.0",
            "requests==2.32.5", "charset-normalizer==3.4.3", "idna==3.10",
            "urllib3==2.5.0", "certifi==2025.8.3"]


def pins(lines: list[str]) -> list[tuple[str, str]]:
    out = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        if req.marker and not Marker(str(req.marker)).evaluate(ENV):
            continue
        (spec,) = req.specifier
        out.append((req.name, spec.version))
    return out


def pypi_files(name: str, version: str) -> list[dict]:
    url = f"https://pypi.org/pypi/{canonicalize_name(name)}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)["urls"]


def manylinux_rank(platform: str, arch: str) -> int | None:
    """Higher is newer; None when the tag does not fit this arch/runtime."""
    if platform == f"manylinux2014_{arch}":
        return 217
    m = re.fullmatch(rf"manylinux_2_(\d+)_{arch}", platform)
    if m and int(m.group(1)) <= 39:  # 25.08's glibc is newer than this
        return 200 + int(m.group(1))
    return None


def pick(name: str, version: str) -> list[dict]:
    """Sources for one package: one universal wheel, or one wheel per arch."""
    files = [f for f in pypi_files(name, version) if f["filename"].endswith(".whl")]
    universal = None
    per_arch: dict[str, tuple[int, dict]] = {}
    for f in files:
        _, _, _, tags = parse_wheel_filename(f["filename"])
        for tag in tags:
            if tag.platform == "any" and tag.interpreter.startswith("py3"):
                universal = f
            if tag.interpreter not in (f"cp{PYTHON}", "cp3", "py3") and tag.abi != "abi3":
                continue
            if tag.abi not in (f"cp{PYTHON}", "abi3", "none"):
                continue
            for arch in ARCHES:
                rank = manylinux_rank(tag.platform, arch)
                if rank is not None and rank > per_arch.get(arch, (0, {}))[0]:
                    per_arch[arch] = (rank, f)
    if universal and not per_arch:
        return [source(universal)]
    missing = set(ARCHES) - set(per_arch)
    if missing:
        sys.exit(f"{name}=={version}: no wheel for {', '.join(sorted(missing))}")
    return [source(f, arch) for arch, (_, f) in sorted(per_arch.items())]


def source(f: dict, arch: str | None = None) -> dict:
    out = {"type": "file", "url": f["url"], "sha256": f["digests"]["sha256"]}
    if arch:
        out["only-arches"] = [arch]
    return out


def module(name: str, packages: list[tuple[str, str]], **extra: object) -> dict:
    sources = [s for n, v in packages for s in pick(n, v)]
    names = " ".join(f'"{n}=={v}"' for n, v in packages)
    return {
        "name": name,
        "buildsystem": "simple",
        "build-commands": [
            "pip3 install --verbose --ignore-installed --no-index --find-links=\"file://${PWD}\" "
            f"--prefix=${{FLATPAK_DEST}} --no-build-isolation {names}"],
        "sources": sources,
        **extra,
    }


def main() -> None:
    app = pins(REQUIREMENTS.read_text().splitlines())
    write(HERE / "python3-deps.json", module("python3-deps", app))
    # Only needed while building wxPython; removed from the finished app.
    write(HERE / "python3-wx-build-deps.json",
          module("python3-wx-build-deps", pins(WX_BUILD), cleanup=["*"]))


def write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {path.relative_to(HERE.parent.parent)} ({len(data['sources'])} files)")


if __name__ == "__main__":
    main()
