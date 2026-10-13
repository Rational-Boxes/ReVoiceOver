"""Left-hand list of pipeline stages with live status and progress."""

from __future__ import annotations

from collections.abc import Callable

import wx
import wx.dataview as dv

from narration import stages
from narration.gui import icons
from narration.stages import Status

REVIEW_ROW = "review"

STATUS_TEXT = {
    Status.MISSING: "",
    Status.DONE: "done",
    Status.STALE: "stale",
    Status.FAILED: "failed",
}
STATUS_ICON = {
    Status.MISSING: "missing",
    Status.DONE: "done",
    Status.STALE: "stale",
    Status.FAILED: "failed",
}


class StagesPanel(wx.Panel):
    COL_NAME, COL_STATUS = range(2)

    def __init__(
        self,
        parent: wx.Window,
        on_run_from: Callable[[str], None],
        on_run_only: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self._on_run_from = on_run_from
        self._on_run_only = on_run_only
        self._busy = False

        # Row keys: every stage, with the review checkpoint after "clean".
        self._rows: list[str] = []
        for name in stages.STAGE_NAMES:
            self._rows.append(name)
            if name == stages.REVIEW_AFTER:
                self._rows.append(REVIEW_ROW)
        self._titles = {s.name: s.title for s in stages.STAGES} | {REVIEW_ROW: "Review script"}

        self.list = dv.DataViewListCtrl(self, style=dv.DV_ROW_LINES | dv.DV_SINGLE)
        self.list.AppendIconTextColumn("Stage", width=160)
        self.list.AppendTextColumn("Status", width=wx.COL_WIDTH_AUTOSIZE)
        for key in self._rows:
            self.list.AppendItem([self._icontext(key, "missing"), ""])

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.list, 1, wx.EXPAND)
        self.SetSizer(sizer)

        self.list.Bind(dv.EVT_DATAVIEW_ITEM_CONTEXT_MENU, self._on_context_menu)
        self.list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self._on_activated)

    # -- display -----------------------------------------------------------
    def _icontext(self, key: str, icon: str) -> dv.DataViewIconText:
        return dv.DataViewIconText(self._titles[key], icons.bundle(icon))

    def _set_row(self, key: str, icon: str, text: str) -> None:
        row = self._rows.index(key)
        self.list.SetValue(self._icontext(key, icon), row, self.COL_NAME)
        self.list.SetValue(text, row, self.COL_STATUS)

    def set_statuses(self, statuses: dict[str, Status], awaiting_review: bool,
                     errors: dict[str, str] | None = None) -> None:
        errors = errors or {}
        for name, status in statuses.items():
            text = STATUS_TEXT[status]
            if status is Status.FAILED and name in errors:
                text = errors[name]
            self._set_row(name, STATUS_ICON[status], text)
        if awaiting_review:
            self._set_row(REVIEW_ROW, "review", "waiting for you")
        else:
            reviewed = statuses[stages.STAGE_NAMES[stages.index(stages.REVIEW_AFTER) + 1]]
            done = reviewed is Status.DONE
            self._set_row(REVIEW_ROW, "done" if done else "missing", "done" if done else "")

    def mark_running(self, stage: str) -> None:
        self._set_row(stage, "running", "running…")

    def set_progress(self, stage: str, done: int, total: int | None, detail: str) -> None:
        text = "running…"
        if total:
            text += f" {max(0, min(100, round(100 * done / total)))}%"
        if detail:
            text += f"  {detail}"
        self.list.SetValue(text, self._rows.index(stage), self.COL_STATUS)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    # -- interaction -------------------------------------------------------
    def _key_for(self, item: dv.DataViewItem) -> str | None:
        if not item.IsOk():
            return None
        key = self._rows[self.list.ItemToRow(item)]
        return stages.STAGE_NAMES[stages.index(stages.REVIEW_AFTER) + 1] \
            if key == REVIEW_ROW else key

    def _on_activated(self, event: dv.DataViewEvent) -> None:
        key = self._key_for(event.GetItem())
        if key and not self._busy:
            self._on_run_from(key)

    def _on_context_menu(self, event: dv.DataViewEvent) -> None:
        key = self._key_for(event.GetItem())
        if key is None:
            return
        menu = wx.Menu()
        run_from = menu.Append(wx.ID_ANY, f"Run from “{self._titles[key]}”")
        run_only = menu.Append(wx.ID_ANY, f"Run only “{self._titles[key]}”")
        for item in (run_from, run_only):
            item.Enable(not self._busy)
        menu.Bind(wx.EVT_MENU, lambda _e: self._on_run_from(key), run_from)
        menu.Bind(wx.EVT_MENU, lambda _e: self._on_run_only(key), run_only)
        self.PopupMenu(menu)
        menu.Destroy()
