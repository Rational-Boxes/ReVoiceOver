"""`narr-gui` entry point: the wx.App, shared recent-files list and window registry."""

from __future__ import annotations

import sys
from pathlib import Path

import wx

APP_NAME = "ReVoiceOver"
MAX_RECENT = 9


class NarrApp(wx.App):
    def OnInit(self) -> bool:  # noqa: N802 (wx naming)
        self.SetAppName(APP_NAME)
        self.SetAppDisplayName(APP_NAME)
        self.config = wx.Config(APP_NAME)
        wx.Config.Set(self.config)
        self.history = wx.FileHistory(MAX_RECENT)
        self.config.SetPath("/Recent")
        self.history.Load(self.config)
        self.config.SetPath("/")
        return True

    def save_history(self) -> None:
        self.config.SetPath("/Recent")
        self.history.Save(self.config)
        self.config.SetPath("/")
        self.config.Flush()

    def frames(self) -> list[wx.Frame]:
        from narration.gui.main_frame import MainFrame

        return [w for w in wx.GetTopLevelWindows() if isinstance(w, MainFrame)]

    def new_window(self) -> wx.Frame:
        from narration.gui.main_frame import MainFrame

        frame = MainFrame(self)
        frame.Show()
        return frame

    def window_for_project(self) -> wx.Frame:
        """An empty window if one exists, else a new one."""
        for frame in self.frames():
            if frame.project is None:
                return frame
        return self.new_window()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    app = NarrApp(redirect=False)
    frame = app.new_window()
    if args:
        target = Path(args[0]).expanduser()
        wx.CallAfter(frame.open_path, target)
    app.MainLoop()


if __name__ == "__main__":
    main()
