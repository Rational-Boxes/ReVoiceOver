#!/usr/bin/env python3
"""Render the app icon (narration/gui/resources/icons/app.svg) for the bundles.

    python packaging/make_icons.py build/icons

writes app.png (1024 px), app.ico (Windows) and app.icns (macOS). wxPython
renders the SVG, exactly as the app does at runtime; Pillow packs the files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import wx
from PIL import Image

SVG = Path(__file__).resolve().parents[1] / "narration/gui/resources/icons/app.svg"


def render(size: int) -> Image.Image:
    image = wx.BitmapBundle.FromSVG(SVG.read_bytes(), wx.Size(size, size)) \
        .GetBitmap(wx.Size(size, size)).ConvertToImage()
    rgb = Image.frombytes("RGB", (size, size), bytes(image.GetData()))
    if image.HasAlpha():
        rgb.putalpha(Image.frombytes("L", (size, size), bytes(image.GetAlpha())))
    return rgb.convert("RGBA")


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/icons")
    out.mkdir(parents=True, exist_ok=True)
    app = wx.App(False)  # noqa: F841 (wx needs an app object to render)
    big = render(1024)
    big.save(out / "app.png")
    # Small sizes are rendered directly, not downscaled, so they stay crisp.
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    images = {s: render(s) for s in ico_sizes}
    images[256].save(out / "app.ico", sizes=[(s, s) for s in ico_sizes],
                     append_images=[images[s] for s in ico_sizes[:-1]])
    big.save(out / "app.icns")
    print(f"wrote {out}/app.png, app.ico, app.icns")


if __name__ == "__main__":
    main()
