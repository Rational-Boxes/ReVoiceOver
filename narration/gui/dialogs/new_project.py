"""New Project dialog: pick a video, a work folder and a few per-video options."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import wx

from narration.config import load_config
from narration.context import default_work_dir
from narration.gui.help_viewer import show_help
from narration.gui.models.project_model import VIDEO_EXTENSIONS

_patterns = ";".join(f"*{e};*{e.upper()}" for e in VIDEO_EXTENSIONS)
VIDEO_WILDCARD = f"Video files|{_patterns}|All files|*"

TIMING_CHOICES = (
    ("anchor", "Anchor — keep narration synced to the original timing (live demos)"),
    ("compact", "Compact — remove dead air, tighter video (slide narration)"),
)


class NewProjectDialog(wx.Dialog):
    def __init__(self, parent: wx.Window | None, video: Path | None = None) -> None:
        super().__init__(parent, title="New Project",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._work_dir_touched = False
        cfg = load_config(video) if video else load_config()

        self.video = wx.FilePickerCtrl(
            self, message="Choose the source video", wildcard=VIDEO_WILDCARD,
            style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL,
        )
        self.work_dir = wx.DirPickerCtrl(self, message="Choose the work folder",
                                         style=wx.DIRP_USE_TEXTCTRL)
        self.timing = wx.RadioBox(self, label="Timing", choices=[t[1] for t in TIMING_CHOICES],
                                  majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.timing.SetSelection([t[0] for t in TIMING_CHOICES].index(cfg.fit.timing))
        # Compact timing: which silences are cut, and how much video stays around speech.
        self.min_silence = self._seconds(cfg.fit.compact_min_silence_s,
                                         "Silences at least this long are cut from the video")
        self.pad_before = self._seconds(cfg.fit.compact_pad_before_s,
                                        "Video kept before speech that follows a cut")
        self.pad_after = self._seconds(cfg.fit.compact_pad_after_s,
                                       "Video kept after speech that precedes a cut")
        self._tts_models = list(dict.fromkeys([cfg.models.tts, cfg.models.tts_alternate]))
        self.tts = wx.Choice(self, choices=self._tts_models)
        self.tts.SetSelection(0)

        form = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        form.AddGrowableCol(1)
        for label, ctrl in (("Video:", self.video), ("Work folder:", self.work_dir),
                            ("Voice model:", self.tts)):
            form.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(ctrl, 1, wx.EXPAND)
        hint = wx.StaticText(self, label="All intermediate files (audio, transcript, script, "
                                         "clips) are kept in the work folder so runs can resume.")
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(form, 0, wx.EXPAND | wx.ALL, 12)
        sizer.Add(hint, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        sizer.Add(self.timing, 0, wx.EXPAND | wx.ALL, 12)
        compact_row = wx.BoxSizer(wx.HORIZONTAL)
        for item in ("Compact: cut silences of at least", self.min_silence, "s, keeping",
                     self.pad_before, "s before and", self.pad_after, "s after speech"):
            ctrl = wx.StaticText(self, label=item) if isinstance(item, str) else item
            compact_row.Add(ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        sizer.Add(compact_row, 0, wx.LEFT | wx.RIGHT, 12)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL | wx.HELP)
        self.FindWindowById(wx.ID_OK).SetLabel("Create")
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(sizer)
        self.SetSize(wx.Size(max(self.GetSize().width, 640), -1))
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()

        self.video.Bind(wx.EVT_FILEPICKER_CHANGED, self._on_video)
        self.work_dir.Bind(wx.EVT_DIRPICKER_CHANGED, self._on_work_dir)
        self.timing.Bind(wx.EVT_RADIOBOX, lambda _e: self._update_compact())
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, lambda _e: show_help("getting-started#new-project"),
                  id=wx.ID_HELP)
        if video:
            self.video.SetPath(str(video))
            self._suggest_work_dir()
        self._update_ok()
        self._update_compact()

    def _seconds(self, value: float, tip: str) -> wx.SpinCtrlDouble:
        ctrl = wx.SpinCtrlDouble(self, min=0.0, max=30.0, initial=value, inc=0.05,
                                 size=wx.Size(90, -1))
        ctrl.SetDigits(2)
        ctrl.SetToolTip(tip)
        return ctrl

    def _compact(self) -> bool:
        return TIMING_CHOICES[self.timing.GetSelection()][0] == "compact"

    def _update_compact(self) -> None:
        for ctrl in (self.min_silence, self.pad_before, self.pad_after):
            ctrl.Enable(self._compact())

    def _on_video(self, _event: wx.Event) -> None:
        self._suggest_work_dir()
        self._update_ok()

    def _on_work_dir(self, _event: wx.Event) -> None:
        self._work_dir_touched = True
        self._update_ok()

    def _suggest_work_dir(self) -> None:
        path = Path(self.video.GetPath())
        if path.is_file() and not self._work_dir_touched:
            self.work_dir.SetPath(str(default_work_dir(path)))

    def _update_ok(self) -> None:
        ok = Path(self.video.GetPath()).is_file() and bool(self.work_dir.GetPath())
        self.FindWindowById(wx.ID_OK).Enable(ok)

    def _on_ok(self, event: wx.CommandEvent) -> None:
        if not Path(self.video.GetPath()).is_file():
            wx.MessageBox("Choose an existing video file.", "New Project",
                          wx.OK | wx.ICON_WARNING, self)
            return
        event.Skip()

    # -- results -----------------------------------------------------------
    def video_path(self) -> Path:
        return Path(self.video.GetPath())

    def work_dir_path(self) -> Path:
        return Path(self.work_dir.GetPath())

    def overrides(self) -> dict[str, dict[str, Any]]:
        return {
            "fit": {"timing": TIMING_CHOICES[self.timing.GetSelection()][0],
                    "compact_min_silence_s": round(self.min_silence.GetValue(), 2),
                    "compact_pad_before_s": round(self.pad_before.GetValue(), 2),
                    "compact_pad_after_s": round(self.pad_after.GetValue(), 2)},
            "models": {"tts": self._tts_models[self.tts.GetSelection()]},
        }
