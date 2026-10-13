"""Light/dark palette for custom-drawn widgets (native widgets follow GTK)."""

from __future__ import annotations

from dataclasses import dataclass

import wx


@dataclass(frozen=True)
class Palette:
    background: wx.Colour
    lane: wx.Colour
    ruler_text: wx.Colour
    grid: wx.Colour
    wave: wx.Colour
    unit: wx.Colour
    unit_text: wx.Colour
    slot: wx.Colour
    selected: wx.Colour
    selected_band: wx.Colour
    playhead: wx.Colour


def is_dark() -> bool:
    return wx.SystemSettings.GetAppearance().IsDark()


def palette() -> Palette:
    if is_dark():
        return Palette(
            background=wx.Colour(30, 31, 34),
            lane=wx.Colour(38, 40, 44),
            ruler_text=wx.Colour(160, 165, 172),
            grid=wx.Colour(58, 61, 66),
            wave=wx.Colour(104, 160, 230),
            unit=wx.Colour(64, 112, 176),
            unit_text=wx.Colour(235, 240, 248),
            slot=wx.Colour(64, 112, 176, 70),
            selected=wx.Colour(245, 190, 70),
            selected_band=wx.Colour(245, 190, 70, 40),
            playhead=wx.Colour(240, 84, 84),
        )
    return Palette(
        background=wx.Colour(250, 250, 251),
        lane=wx.Colour(240, 241, 244),
        ruler_text=wx.Colour(96, 100, 108),
        grid=wx.Colour(222, 224, 229),
        wave=wx.Colour(47, 127, 216),
        unit=wx.Colour(84, 140, 214),
        unit_text=wx.Colour(255, 255, 255),
        slot=wx.Colour(84, 140, 214, 60),
        selected=wx.Colour(214, 140, 20),
        selected_band=wx.Colour(214, 140, 20, 36),
        playhead=wx.Colour(214, 69, 69),
    )
