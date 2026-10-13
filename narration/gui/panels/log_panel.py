"""Run log with a level filter and a copy-to-clipboard button."""

from __future__ import annotations

import logging
from datetime import datetime

import wx

from narration.credentials import mask

LEVELS = (("Debug", logging.DEBUG), ("Info", logging.INFO),
          ("Warnings", logging.WARNING), ("Errors", logging.ERROR))
MAX_LINES = 20_000


class LogPanel(wx.Panel):
    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent)
        self._lines: list[tuple[int, str]] = []

        self.level = wx.Choice(self, choices=[label for label, _ in LEVELS])
        self.level.SetSelection(1)
        copy_btn = wx.Button(self, label="Copy diagnostics")
        clear_btn = wx.Button(self, label="Clear")
        self.text = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
                                | wx.TE_DONTWRAP)
        self.text.SetFont(wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE)))

        bar = wx.BoxSizer(wx.HORIZONTAL)
        bar.Add(wx.StaticText(self, label="Show:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        bar.Add(self.level, 0, wx.ALIGN_CENTER_VERTICAL)
        bar.AddStretchSpacer()
        bar.Add(copy_btn, 0, wx.RIGHT, 6)
        bar.Add(clear_btn)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(bar, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(self.text, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self.level.Bind(wx.EVT_CHOICE, lambda _e: self._refill())
        copy_btn.Bind(wx.EVT_BUTTON, self._on_copy)
        clear_btn.Bind(wx.EVT_BUTTON, lambda _e: self.clear())

    @property
    def min_level(self) -> int:
        return LEVELS[self.level.GetSelection()][1]

    def _colour(self, level: int) -> wx.Colour:
        if level >= logging.ERROR:
            return wx.Colour(214, 69, 69)
        if level >= logging.WARNING:
            return wx.Colour(217, 154, 30)
        if level < logging.INFO:
            return wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)
        return wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)

    def _write(self, level: int, line: str) -> None:
        self.text.SetDefaultStyle(wx.TextAttr(self._colour(level)))
        self.text.AppendText(line + "\n")

    def append(self, level: int, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {mask(msg)}"
        self._lines.append((level, line))
        if len(self._lines) > MAX_LINES:
            del self._lines[: MAX_LINES // 10]
            self._refill()
        elif level >= self.min_level:
            self._write(level, line)

    def _refill(self) -> None:
        self.text.Freeze()
        try:
            self.text.Clear()
            for level, line in self._lines:
                if level >= self.min_level:
                    self._write(level, line)
        finally:
            self.text.Thaw()

    def clear(self) -> None:
        self._lines.clear()
        self.text.Clear()

    def _on_copy(self, _event: wx.CommandEvent) -> None:
        text = "\n".join(f"{logging.getLevelName(lv):<7} {line}" for lv, line in self._lines)
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(text))
            finally:
                wx.TheClipboard.Close()
