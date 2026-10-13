"""`narr-gui` entry point: the wx.App, shared recent-files list and window registry."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import wx

from narration import credentials

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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="narr-gui", description="ReVoiceOver desktop app")
    parser.add_argument("path", nargs="?", type=Path,
                        help="a video to start a project for, or a project work folder")
    parser.add_argument("--env-file", type=Path,
                        help="read DEEPINFRA_TOKEN / DEEPINFRA_KEY from this file "
                             "instead of searching for .env")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.env_file is not None:
        try:
            credentials.use_env_file(args.env_file.expanduser())
        except FileNotFoundError as exc:
            sys.exit(f"narr-gui: {exc}")
    app = NarrApp(redirect=False)
    frame = app.new_window()
    if args.path is not None:
        wx.CallAfter(frame.open_path, args.path.expanduser())
    app.MainLoop()


if __name__ == "__main__":
    main()
