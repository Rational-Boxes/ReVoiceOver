"""Empty-window page: start a project, open one, or drop a video."""

from __future__ import annotations

from collections.abc import Callable

import wx

from narration.gui import icons


class WelcomePanel(wx.Panel):
    def __init__(self, parent: wx.Window, on_new: Callable[[], None],
                 on_open: Callable[[], None]) -> None:
        super().__init__(parent)
        logo = wx.StaticBitmap(self, bitmap=icons.bundle("app", wx.Size(72, 72)))
        title = wx.StaticText(self, label="ReVoiceOver")
        title.SetFont(title.GetFont().Scaled(2.0).Bold())
        subtitle = wx.StaticText(
            self, label="Clean up a recorded narration and re-voice it in your own cloned voice.")
        hint = wx.StaticText(self, label="Drop a video file here, or:")
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

        new_btn = wx.Button(self, label="New Project…")
        new_btn.SetBitmap(icons.bundle("new"))
        open_btn = wx.Button(self, label="Open Project…")
        open_btn.SetBitmap(icons.bundle("open"))
        new_btn.Bind(wx.EVT_BUTTON, lambda _e: on_new())
        open_btn.Bind(wx.EVT_BUTTON, lambda _e: on_open())

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(new_btn, 0, wx.RIGHT, 10)
        buttons.Add(open_btn)

        col = wx.BoxSizer(wx.VERTICAL)
        col.Add(logo, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)
        col.Add(title, 0, wx.ALIGN_CENTER | wx.BOTTOM, 6)
        col.Add(subtitle, 0, wx.ALIGN_CENTER | wx.BOTTOM, 24)
        col.Add(hint, 0, wx.ALIGN_CENTER | wx.BOTTOM, 10)
        col.Add(buttons, 0, wx.ALIGN_CENTER)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.AddStretchSpacer()
        outer.Add(col, 0, wx.ALIGN_CENTER | wx.ALL, 24)
        outer.AddStretchSpacer(2)
        self.SetSizer(outer)
