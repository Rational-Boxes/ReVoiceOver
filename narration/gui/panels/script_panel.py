"""Script review: filterable unit list above the unit editor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import wx
import wx.dataview as dv

from narration.gui import icons
from narration.gui.help_viewer import help_button
from narration.gui.models.script_model import ScriptModel
from narration.gui.models.timeline_model import format_time
from narration.gui.panels.unit_editor import EditorCallbacks, UnitEditor
from narration.stages.synth import unit_clips
from narration.types import Unit

PAGE_EMPTY, PAGE_SCRIPT = 0, 1
COL_FLAG, COL_ID, COL_START, COL_SLOT, COL_TEXT, COL_RATIO = range(6)


@dataclass
class ScriptCallbacks:
    select: Callable[[str], None]          # keep the timeline in sync
    play_original: Callable[[str], None]
    play_tts: Callable[[str], None]
    new_take: Callable[[str], None]
    approve: Callable[[], None]
    changed: Callable[[], None]            # dirty state or text changed


class ScriptPanel(wx.Panel):
    def __init__(self, parent: wx.Window, callbacks: ScriptCallbacks) -> None:
        super().__init__(parent)
        self.cb = callbacks
        self.model = ScriptModel()
        self.clips: dict[str, dict[str, Any]] = {}  # each unit's speech-group clip
        self._rows: list[str] = []
        self._selecting = False

        self.book = wx.Simplebook(self)
        empty = wx.Panel(self.book)
        msg = wx.StaticText(empty, label="The script appears here once the “Clean script” stage "
                                         "has run.\nRun the pipeline to get there.",
                            style=wx.ALIGN_CENTER_HORIZONTAL)
        msg.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        es = wx.BoxSizer(wx.VERTICAL)
        es.AddStretchSpacer()
        es.Add(msg, 0, wx.ALIGN_CENTER)
        es.AddStretchSpacer()
        empty.SetSizer(es)

        page = wx.Panel(self.book)
        self.search = wx.SearchCtrl(page, size=wx.Size(220, -1))
        self.search.SetDescriptiveText("Search text or id")
        self.flagged_cb = wx.CheckBox(page, label="Flagged only")
        self.changed_cb = wx.CheckBox(page, label="Changed only")
        self.stats = wx.StaticText(page)
        self.save_btn = wx.Button(page, wx.ID_SAVE, "Save")
        self.approve_btn = wx.Button(page, label="Approve && Continue")
        self.approve_btn.SetBitmap(icons.bundle("run"))
        self.approve_btn.SetToolTip("Save the script and run the remaining stages")

        bar = wx.BoxSizer(wx.HORIZONTAL)
        bar.Add(self.search, 0, wx.RIGHT, 10)
        bar.Add(self.flagged_cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        bar.Add(self.changed_cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        bar.Add(self.stats, 1, wx.ALIGN_CENTER_VERTICAL)
        bar.Add(self.save_btn, 0, wx.RIGHT, 6)
        bar.Add(self.approve_btn, 0, wx.RIGHT, 6)
        bar.Add(help_button(page, "script-review#list", "reviewing the script"), 0,
                wx.ALIGN_CENTER_VERTICAL)

        self.splitter = wx.SplitterWindow(page, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH)
        self.list = dv.DataViewListCtrl(self.splitter, style=dv.DV_ROW_LINES | dv.DV_SINGLE)
        self.list.AppendIconTextColumn("", width=34)
        self.list.AppendTextColumn("Unit", width=62)
        self.list.AppendTextColumn("Start", width=62, align=wx.ALIGN_RIGHT)
        self.list.AppendTextColumn("Slot", width=52, align=wx.ALIGN_RIGHT)
        self.list.AppendTextColumn("Narration", width=520)
        self.list.AppendTextColumn("Length", width=64, align=wx.ALIGN_RIGHT)
        self.editor = UnitEditor(self.splitter, EditorCallbacks(
            commit=self._commit_text,
            play_original=self.cb.play_original,
            keep_original=lambda uid, v: self._change(uid, "keep_original", v),
            revert_llm=lambda uid: self._after_edit(uid, self.model.revert_to_llm(uid)),
            use_raw=lambda uid: self._after_edit(uid, self.model.revert_to_raw(uid)),
            play_tts=self.cb.play_tts,
            new_take=self.cb.new_take,
            next_unit=lambda: self.move(1),
            back_to_list=self.list.SetFocus,
        ))
        self.splitter.SplitHorizontally(self.list, self.editor, -300)
        self.splitter.SetMinimumPaneSize(160)
        self.splitter.SetSashGravity(1.0)

        ps = wx.BoxSizer(wx.VERTICAL)
        ps.Add(bar, 0, wx.EXPAND | wx.ALL, 6)
        ps.Add(self.splitter, 1, wx.EXPAND)
        page.SetSizer(ps)

        self.book.AddPage(empty, "empty")
        self.book.AddPage(page, "script")
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.book, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self.search.Bind(wx.EVT_TEXT, lambda _e: self._refill())
        self.flagged_cb.Bind(wx.EVT_CHECKBOX, lambda _e: self._refill())
        self.changed_cb.Bind(wx.EVT_CHECKBOX, lambda _e: self._refill())
        self.save_btn.Bind(wx.EVT_BUTTON, lambda _e: self.save())
        self.approve_btn.Bind(wx.EVT_BUTTON, lambda _e: self.cb.approve())
        self.list.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_selection)
        self.list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, lambda _e: self.editor.focus_text())
        self.list.Bind(wx.EVT_CHAR_HOOK, self._on_list_key)

    # -- load / save -------------------------------------------------------
    def load(self, work_dir: Path, video_name: str, glossary: list[str]) -> None:
        selected = self.selected_id()
        has_script = self.model.load(work_dir, video_name)
        self.load_clips()
        self.editor.glossary = glossary
        self.book.SetSelection(PAGE_SCRIPT if has_script else PAGE_EMPTY)
        self._refill()
        if selected and self.model.get(selected):
            self.select(selected)
        self.cb.changed()

    def load_clips(self) -> None:
        if self.model.work_dir is None:
            return
        self.clips = unit_clips(self.model.work_dir)
        if self.editor.unit is not None:
            self._show(self.editor.unit.id)

    def _show(self, unit_id: str) -> None:
        self.editor.show(self.model.get(unit_id), self.clips.get(unit_id),
                         self.model.qa.get(unit_id))

    def reload_results(self) -> None:
        """Pick up new synth/QA results without touching unsaved script edits."""
        self.model.load_qa()
        self.load_clips()
        for uid in self._rows:
            self._refresh_row(uid)
        self._update_stats()

    def save(self) -> None:
        self.editor.commit()
        self.model.save()
        self._update_stats()
        self.cb.changed()

    @property
    def dirty(self) -> bool:
        return self.model.dirty or self.editor.pending

    def set_busy(self, busy: bool) -> None:
        """Lock editing while a pipeline job may rewrite script.json."""
        self.editor.clean.Enable(not busy)
        self.editor.set_busy(busy)
        self.approve_btn.Enable(not busy and self.model.loaded)

    # -- list --------------------------------------------------------------
    def _row_values(self, u: Unit) -> list[object]:
        flags = self.model.all_flags(u)
        severe = "dropped-content" in flags or any(f.startswith("qa-") for f in flags)
        icon = "failed" if severe else "stale" if flags else "done" if u.edited else "missing"
        icon_text = dv.DataViewIconText("", icons.bundle(icon)) if (flags or u.edited) \
            else dv.DataViewIconText("")
        ratio = len(u.clean_text or "") / max(1, len(u.raw_text))
        text = u.clean_text if u.clean_text else "(dropped)"
        return [icon_text, u.id, format_time(u.start, 0.1), f"{u.slot:.1f}s", text, f"{ratio:.0%}"]

    def _refill(self) -> None:
        selected = self.selected_id()
        units = self.model.filtered(self.search.GetValue(), self.flagged_cb.GetValue(),
                                    self.changed_cb.GetValue())
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            self._rows = [u.id for u in units]
            for u in units:
                self.list.AppendItem(self._row_values(u))
        finally:
            self.list.Thaw()
        self._update_stats()
        if selected in self._rows:
            self.select(selected)
        else:
            self.editor.show(None)

    def _refresh_row(self, unit_id: str) -> None:
        unit = self.model.get(unit_id)
        if unit is None or unit_id not in self._rows:
            return
        row = self._rows.index(unit_id)
        for col, value in enumerate(self._row_values(unit)):
            self.list.SetValue(value, row, col)

    def _update_stats(self) -> None:
        s = self.model.stats()
        shown = len(self._rows)
        prefix = f"{shown} of {s['units']}" if shown != s["units"] else f"{s['units']}"
        self.stats.SetLabel(f"{prefix} units · {s['flagged']} flagged · {s['edited']} edited"
                            + (" · unsaved" if self.model.dirty else ""))
        self.save_btn.Enable(self.model.dirty)
        self.approve_btn.Enable(self.model.loaded)

    def selected_id(self) -> str | None:
        row = self.list.GetSelectedRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def select(self, unit_id: str) -> None:
        """Select ``unit_id`` (from the timeline); clears filters that hide it."""
        if unit_id not in self._rows and self.model.get(unit_id):
            self.search.ChangeValue("")
            self.flagged_cb.SetValue(False)
            self.changed_cb.SetValue(False)
            self._refill()
        if unit_id not in self._rows:
            return
        row = self._rows.index(unit_id)
        self._selecting = True
        try:
            self.list.SelectRow(row)
            self.list.EnsureVisible(self.list.RowToItem(row))
        finally:
            self._selecting = False
        self.editor.commit()
        self._show(unit_id)

    def move(self, step: int) -> None:
        row = self.list.GetSelectedRow()
        target = min(max(0, row + step), len(self._rows) - 1) if self._rows else -1
        if target >= 0:
            self.select(self._rows[target])
            self.cb.select(self._rows[target])

    def _on_selection(self, _event: dv.DataViewEvent) -> None:
        if self._selecting:
            return
        self.editor.commit()
        unit_id = self.selected_id()
        if unit_id:
            self._show(unit_id)
        else:
            self.editor.show(None)
        if unit_id:
            self.cb.select(unit_id)

    def _on_list_key(self, event: wx.KeyEvent) -> None:
        if self.FindFocus() is not self.list.GetMainWindow() and \
                self.FindFocus() is not self.list:
            event.Skip()
            return
        key = event.GetKeyCode()
        if key == wx.WXK_SPACE and (uid := self.selected_id()):
            (self.cb.play_tts if event.ControlDown() else self.cb.play_original)(uid)
        elif key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and self.selected_id():
            self.editor.focus_text()
        else:
            event.Skip()

    # -- edits -------------------------------------------------------------
    def _commit_text(self, unit_id: str, text: str) -> None:
        self._after_edit(unit_id, self.model.set_text(unit_id, text))

    def _change(self, unit_id: str, field: str, value: object) -> None:
        self._after_edit(unit_id, self.model.set(unit_id, field, value))

    def _after_edit(self, unit_id: str, changed: bool) -> None:
        if not changed:
            return
        self._refresh_row(unit_id)
        if self.editor.unit is not None and self.editor.unit.id == unit_id:
            self._show(unit_id)
        self._update_stats()
        self.cb.changed()

    def undo(self) -> None:
        if self.editor.clean.HasFocus() and self.editor.clean.CanUndo():
            self.editor.clean.Undo()
        elif (uid := self.model.undo()) is not None:
            self._after_edit(uid, True)
            self.select(uid)

    def redo(self) -> None:
        if self.editor.clean.HasFocus() and self.editor.clean.CanRedo():
            self.editor.clean.Redo()
        elif (uid := self.model.redo()) is not None:
            self._after_edit(uid, True)
            self.select(uid)
