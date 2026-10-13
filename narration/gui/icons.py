"""Bundled SVG icons as HiDPI-aware ``wx.BitmapBundle``s."""

from __future__ import annotations

import functools
from importlib import resources

import wx

TOOLBAR = wx.Size(22, 22)
LIST = wx.Size(16, 16)


@functools.cache
def _svg(name: str) -> bytes:
    return (resources.files("narration.gui") / "resources" / "icons" / f"{name}.svg").read_bytes()


def bundle(name: str, size: wx.Size = LIST) -> wx.BitmapBundle:
    return wx.BitmapBundle.FromSVG(_svg(name), size)


def app_icon() -> wx.IconBundle:
    icons = wx.IconBundle()
    for px in (16, 32, 48, 64, 128):
        icons.AddIcon(wx.Icon(bundle("app", wx.Size(px, px)).GetBitmap(wx.Size(px, px))))
    return icons
