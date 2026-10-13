"""Voice tab: the cloned voice's reference clip, alternatives, and registered voices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import wx
import wx.dataview as dv

from narration import artifacts
from narration.gui import icons
from narration.gui.models.timeline_model import format_time
from narration.stages.voice import REF_FILE, load_registry

AUDIO_WILDCARD = "Audio files|*.wav;*.mp3;*.flac;*.m4a;*.ogg|All files|*"


@dataclass
class VoiceCallbacks:
    play_file: Callable[[Path], None]
    play_span: Callable[[float, float], None]   # from the source recording
    use_span: Callable[[float, float], None]
    use_file: Callable[[Path], None]
    use_auto: Callable[[], None]


class VoicePanel(wx.Panel):
    def __init__(self, parent: wx.Window, callbacks: VoiceCallbacks) -> None:
        super().__init__(parent)
        self.cb = callbacks
        self.work_dir: Path | None = None
        self.candidates: list[dict[str, Any]] = []

        title = wx.StaticText(self, label="Cloned voice")
        title.SetFont(title.GetFont().Bold().Scaled(1.15))
        self.summary = wx.StaticText(self)
        self.play_ref = wx.Button(self, label="Play Reference Clip")
        self.play_ref.SetBitmap(icons.bundle("run"))
        self.choose_file = wx.Button(self, label="Use My Own Clip…")
        self.auto_btn = wx.Button(self, label="Pick Automatically")
        hint = wx.StaticText(self, label="Changing the reference registers a new voice; the "
                                         "synthesized clips then become stale.")
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

        alt_label = wx.StaticText(self, label="Other clean stretches of your recording")
        alt_label.SetFont(alt_label.GetFont().Bold())
        self.alts = dv.DataViewListCtrl(self, style=dv.DV_ROW_LINES | dv.DV_SINGLE,
                                        size=wx.Size(-1, 120))
        self.alts.AppendTextColumn("From", width=70)
        self.alts.AppendTextColumn("To", width=70)
        self.alts.AppendTextColumn("Score", width=60, align=wx.ALIGN_RIGHT)
        self.alts.AppendTextColumn("Words", width=600)
        self.play_alt = wx.Button(self, label="Play")
        self.play_alt.SetBitmap(icons.bundle("run"))
        self.use_alt = wx.Button(self, label="Use This One")

        reg_label = wx.StaticText(self, label="Voices registered on this computer")
        reg_label.SetFont(reg_label.GetFont().Bold())
        self.registry = dv.DataViewListCtrl(self, style=dv.DV_ROW_LINES | dv.DV_SINGLE,
                                            size=wx.Size(-1, 90))
        self.registry.AppendTextColumn("Voice id", width=200)
        self.registry.AppendTextColumn("From video", width=600)

        ref_row = wx.BoxSizer(wx.HORIZONTAL)
        for btn in (self.play_ref, self.choose_file, self.auto_btn):
            ref_row.Add(btn, 0, wx.RIGHT, 6)
        alt_row = wx.BoxSizer(wx.HORIZONTAL)
        alt_row.Add(self.play_alt, 0, wx.RIGHT, 6)
        alt_row.Add(self.use_alt)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(title, 0, wx.ALL, 10)
        sizer.Add(self.summary, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        sizer.Add(ref_row, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(hint, 0, wx.ALL, 10)
        sizer.Add(alt_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(self.alts, 1, wx.EXPAND | wx.ALL, 10)
        sizer.Add(alt_row, 0, wx.LEFT | wx.RIGHT, 10)
        sizer.Add(reg_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.Add(self.registry, 1, wx.EXPAND | wx.ALL, 10)
        self.SetSizer(sizer)

        self.play_ref.Bind(wx.EVT_BUTTON, self._on_play_ref)
        self.choose_file.Bind(wx.EVT_BUTTON, self._on_choose_file)
        self.auto_btn.Bind(wx.EVT_BUTTON, lambda _e: self.cb.use_auto())
        self.play_alt.Bind(wx.EVT_BUTTON, lambda _e: self._with_alt(self.cb.play_span))
        self.use_alt.Bind(wx.EVT_BUTTON, lambda _e: self._with_alt(self.cb.use_span))
        self.alts.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, lambda _e: self._update_buttons())
        self.alts.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED,
                       lambda _e: self._with_alt(self.cb.play_span))
        self.load(None)

    def load(self, work_dir: Path | None) -> None:
        self.work_dir = work_dir
        voice: dict[str, Any] = {}
        if work_dir is not None:
            try:
                voice = artifacts.read_json(work_dir / "voice.json")
            except (FileNotFoundError, artifacts.SchemaError):
                voice = {}
        if voice:
            span = voice.get("ref_span")
            where = (f"from {format_time(span[0], 0.1)}–{format_time(span[1], 0.1)} of the "
                     "recording" if span else "from your own clip")
            self.summary.SetLabel(f"Voice {voice['voice_id']} ({voice.get('model', '')}), "
                                  f"cloned {where}"
                                  + (" — reused from an earlier registration"
                                     if voice.get("reused") else ""))
        else:
            self.summary.SetLabel("No voice yet. It is chosen and registered by the “Voice” "
                                  "stage after you approve the script.")
        self.candidates = list(voice.get("candidates", []))
        self.alts.DeleteAllItems()
        for c in self.candidates:
            self.alts.AppendItem([format_time(c["start"], 0.1), format_time(c["end"], 0.1),
                                  f"{c['score']:.2f}", c["text"]])
        self.registry.DeleteAllItems()
        for entry in load_registry().values():
            self.registry.AppendItem([entry.get("voice_id", ""), entry.get("source", "")])
        self.play_ref.Enable(bool(voice) and work_dir is not None
                             and (work_dir / REF_FILE).exists())
        self.choose_file.Enable(work_dir is not None)
        self.auto_btn.Enable(work_dir is not None)
        self._update_buttons()
        self.Layout()

    def _selected_alt(self) -> dict[str, Any] | None:
        row = self.alts.GetSelectedRow()
        return self.candidates[row] if 0 <= row < len(self.candidates) else None

    def _with_alt(self, fn: Callable[[float, float], None]) -> None:
        if (alt := self._selected_alt()) is not None:
            fn(float(alt["start"]), float(alt["end"]))

    def _update_buttons(self) -> None:
        has = self._selected_alt() is not None
        self.play_alt.Enable(has)
        self.use_alt.Enable(has)

    def _on_play_ref(self, _event: wx.CommandEvent) -> None:
        if self.work_dir is not None:
            self.cb.play_file(self.work_dir / REF_FILE)

    def _on_choose_file(self, _event: wx.CommandEvent) -> None:
        with wx.FileDialog(self, "Choose a 10–15 s clip of your voice", wildcard=AUDIO_WILDCARD,
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.cb.use_file(Path(dlg.GetPath()))
