"""QA tab: synthesized clips whose words didn't match, with fixes at hand."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import wx
import wx.dataview as dv

from narration.gui import icons
from narration.gui.help_viewer import help_button
from narration.gui.models.script_model import ScriptModel


@dataclass
class QACallbacks:
    play_tts: Callable[[str], None]
    new_take: Callable[[str], None]
    edit: Callable[[str], None]        # jump to the unit in the Script tab
    accept: Callable[[str], None]
    select: Callable[[str], None]


class QAPanel(wx.Panel):
    def __init__(self, parent: wx.Window, callbacks: QACallbacks) -> None:
        super().__init__(parent)
        self.cb = callbacks
        self._rows: list[str] = []  # first unit id of each listed speech group

        self.summary = wx.StaticText(self)
        self.list = dv.DataViewListCtrl(self, style=dv.DV_ROW_LINES | dv.DV_SINGLE)
        self.list.AppendTextColumn("Units", width=110)
        self.list.AppendTextColumn("WER", width=56, align=wx.ALIGN_RIGHT)
        self.list.AppendTextColumn("Tries", width=50, align=wx.ALIGN_RIGHT)
        self.list.AppendTextColumn("Problem", width=130)
        self.list.AppendTextColumn("Script says", width=330)
        self.list.AppendTextColumn("Whisper heard", width=330)

        self.play_btn = wx.Button(self, label="Play Synthesized")
        self.play_btn.SetBitmap(icons.bundle("run"))
        self.take_btn = wx.Button(self, label="New Take")
        self.edit_btn = wx.Button(self, label="Edit Text…")
        self.accept_btn = wx.Button(self, label="Accept Anyway")
        tip = wx.StaticText(self, label="Tip: acronyms the voice can't say (e.g. “AIA”) are best "
                                        "fixed in pronunciations.toml next to the video.")
        tip.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for b in (self.play_btn, self.take_btn, self.edit_btn):
            buttons.Add(b, 0, wx.RIGHT, 6)
        buttons.AddStretchSpacer()
        buttons.Add(self.accept_btn)
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(self.summary, 1, wx.ALIGN_CENTER_VERTICAL)
        top.Add(help_button(self, "quality", "the quality check"), 0, wx.ALIGN_CENTER_VERTICAL)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(top, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        tip_row = wx.BoxSizer(wx.HORIZONTAL)
        tip_row.Add(tip, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        tip_row.Add(help_button(self, "quality#pronunciations", "pronunciation fixes"), 0,
                    wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(tip_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(sizer)

        for btn, fn in ((self.play_btn, self.cb.play_tts), (self.take_btn, self.cb.new_take),
                        (self.edit_btn, self.cb.edit), (self.accept_btn, self.cb.accept)):
            btn.Bind(wx.EVT_BUTTON, lambda _e, fn=fn: self._with_selected(fn))
        self.list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_selection)
        self.list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED,
                       lambda _e: self._with_selected(self.cb.play_tts))
        self.busy = False
        self._update_buttons()

    def load(self, model: ScriptModel) -> None:
        selected = self.selected_id()
        self.list.DeleteAllItems()
        self._rows = []
        failures = model.qa_failing_groups()
        for _gid, entry in failures:
            units = entry["units"]
            span = units[0] if len(units) == 1 else f"{units[0]}–{units[-1]}"
            text = " ".join((u.clean_text or "") for uid in units if (u := model.get(uid)))
            self._rows.append(units[0])
            self.list.AppendItem([span, f"{entry['wer']:.0%}", str(entry.get("retries", 0)),
                                  ", ".join(entry.get("flags", [])), text,
                                  entry.get("heard", "")])
        checked = model.qa_checked()
        if not checked:
            self.summary.SetLabel("Quality check has not run yet.")
        elif failures:
            self.summary.SetLabel(f"{len(failures)} of {checked} speech groups need a look. "
                                  "Listen, then make a new take, change the text, or accept.")
        else:
            self.summary.SetLabel(f"All {checked} speech groups passed the quality check.")
        if selected in self._rows:
            self.list.SelectRow(self._rows.index(selected))
        self._update_buttons()
        self.Layout()

    def selected_id(self) -> str | None:
        row = self.list.GetSelectedRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._update_buttons()

    def _with_selected(self, fn: Callable[[str], None]) -> None:
        if (uid := self.selected_id()) is not None:
            fn(uid)

    def _on_selection(self, _event: dv.DataViewEvent) -> None:
        self._update_buttons()
        if (uid := self.selected_id()) is not None:
            self.cb.select(uid)

    def _update_buttons(self) -> None:
        has = self.selected_id() is not None
        for btn in (self.play_btn, self.edit_btn, self.accept_btn):
            btn.Enable(has)
        self.take_btn.Enable(has and not self.busy)
