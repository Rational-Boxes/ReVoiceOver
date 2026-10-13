"""Main project window: AUI layout, menus, toolbar, status bar, run control."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import wx
import wx.adv
import wx.aui as aui

from narration import stages
from narration.gui import events, icons
from narration.gui.dialogs.new_project import NewProjectDialog
from narration.gui.dialogs.token import TokenDialog
from narration.gui.models.project_model import (
    VIDEO_EXTENSIONS,
    Project,
    ProjectError,
    is_project_dir,
)
from narration.gui.panels.log_panel import LogPanel
from narration.gui.panels.stages_panel import StagesPanel
from narration.gui.runner import PipelineRunner
from narration.gui.timeline_controller import TimelineController
from narration.gui.welcome import WelcomePanel
from narration.pipeline import Outcome, RunResult

if TYPE_CHECKING:
    from narration.gui.app import NarrApp

PAGE_WELCOME, PAGE_PROJECT = 0, 1
FIELD_MSG, FIELD_GAUGE, FIELD_PATH = range(3)
GAUGE_WIDTH = 160
LAYOUT_VERSION = 2  # bump when panes are added so stale perspectives are ignored
TIMELINE_STAGES = {"extract", "segment", "clean"}


class _DropTarget(wx.FileDropTarget):
    def __init__(self, frame: MainFrame) -> None:
        super().__init__()
        self.frame = frame

    def OnDropFiles(self, x: int, y: int, filenames: list[str]) -> bool:  # noqa: N802
        if filenames:
            wx.CallAfter(self.frame.open_path, Path(filenames[0]))
        return True


class MainFrame(wx.Frame):
    def __init__(self, app: NarrApp) -> None:
        super().__init__(None, title="ReVoiceOver", size=wx.Size(1200, 780))
        self.app = app
        self.project: Project | None = None
        self.SetIcons(icons.app_icon())
        self.SetMinSize(wx.Size(760, 480))

        self.reporter = events.WxReporter(self)
        self.runner = PipelineRunner(self.reporter, self._finished_from_worker)
        self._current_stage: str | None = None

        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()
        self._build_panes()
        self._bind_events()
        self.SetDropTarget(_DropTarget(self))
        self._restore_geometry()
        self._update_ui()

    # ------------------------------------------------------------------ build
    def _build_menus(self) -> None:
        bar = wx.MenuBar()

        file_menu = wx.Menu()
        file_menu.Append(wx.ID_NEW, "&New Project…\tCtrl+N")
        file_menu.Append(wx.ID_OPEN, "&Open Project…\tCtrl+O")
        self.recent_menu = wx.Menu()
        file_menu.AppendSubMenu(self.recent_menu, "Open &Recent")
        self.app.history.UseMenu(self.recent_menu)
        self.app.history.AddFilesToMenu(self.recent_menu)
        file_menu.AppendSeparator()
        self.id_show_folder = wx.NewIdRef()
        file_menu.Append(self.id_show_folder, "Show &Work Folder")
        file_menu.AppendSeparator()
        file_menu.Append(wx.ID_CLOSE, "&Close Window\tCtrl+W")
        file_menu.Append(wx.ID_EXIT, "&Quit\tCtrl+Q")
        bar.Append(file_menu, "&File")

        run_menu = wx.Menu()
        self.id_run = wx.NewIdRef()
        self.id_cancel = wx.NewIdRef()
        self.id_continue = wx.NewIdRef()
        run_menu.Append(self.id_run, "&Run\tCtrl+R", "Run the pipeline from the first "
                                                    "stage that is not done")
        run_menu.Append(self.id_continue, "Approve Script && &Continue\tCtrl+Shift+R")
        self.run_from_menu = wx.Menu()
        self._run_from_ids: dict[int, str] = {}
        for stage in stages.STAGES:
            item = self.run_from_menu.Append(wx.ID_ANY, stage.title)
            self._run_from_ids[item.GetId()] = stage.name
        run_menu.AppendSubMenu(self.run_from_menu, "Run &From")
        run_menu.AppendSeparator()
        run_menu.Append(self.id_cancel, "C&ancel\tCtrl+.")
        bar.Append(run_menu, "&Run")

        view_menu = wx.Menu()
        self.id_reset_layout = wx.NewIdRef()
        view_menu.Append(self.id_reset_layout, "&Reset Layout")
        bar.Append(view_menu, "&View")

        settings_menu = wx.Menu()
        self.id_token = wx.NewIdRef()
        settings_menu.Append(self.id_token, "DeepInfra &Token…")
        bar.Append(settings_menu, "&Settings")

        help_menu = wx.Menu()
        help_menu.Append(wx.ID_ABOUT, "&About ReVoiceOver")
        bar.Append(help_menu, "&Help")
        self.SetMenuBar(bar)

    def _build_toolbar(self) -> None:
        tb = self.CreateToolBar(wx.TB_HORIZONTAL | wx.TB_TEXT | wx.TB_HORZ_LAYOUT | wx.TB_FLAT)
        tb.SetToolBitmapSize(icons.TOOLBAR)
        tb.AddTool(wx.ID_NEW, "New", icons.bundle("new", icons.TOOLBAR), "New project")
        tb.AddTool(wx.ID_OPEN, "Open", icons.bundle("open", icons.TOOLBAR), "Open project")
        tb.AddSeparator()
        tb.AddTool(self.id_run, "Run", icons.bundle("run", icons.TOOLBAR),
                   "Run from the first stage that is not done")
        self.id_run_from_tool = wx.NewIdRef()
        tb.AddTool(self.id_run_from_tool, "Run From", icons.bundle("run-from", icons.TOOLBAR),
                   "Run from a chosen stage", wx.ITEM_DROPDOWN)
        tb.SetDropdownMenu(self.id_run_from_tool, self._make_run_from_menu())
        tb.AddTool(self.id_cancel, "Cancel", icons.bundle("cancel", icons.TOOLBAR),
                   "Cancel the running job")
        tb.Realize()
        self.toolbar = tb

    def _make_run_from_menu(self) -> wx.Menu:
        menu = wx.Menu()
        self._run_from_ids_tb: dict[int, str] = {}
        for stage in stages.STAGES:
            item = menu.Append(wx.ID_ANY, stage.title)
            self._run_from_ids_tb[item.GetId()] = stage.name
            menu.Bind(wx.EVT_MENU, self._on_run_from_menu, item)
        return menu

    def _build_statusbar(self) -> None:
        sb = self.CreateStatusBar(3)
        sb.SetStatusWidths([-1, GAUGE_WIDTH, -1])
        self.gauge = wx.Gauge(sb, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        self.gauge.Hide()
        sb.Bind(wx.EVT_SIZE, self._on_statusbar_size)

    def _build_panes(self) -> None:
        self.aui = aui.AuiManager(self, aui.AUI_MGR_DEFAULT | aui.AUI_MGR_LIVE_RESIZE)

        self.stages_panel = StagesPanel(self, self.run_from, self.run_only)
        self.aui.AddPane(self.stages_panel, aui.AuiPaneInfo().Name("stages").Caption("Stages")
                         .Left().BestSize(300, -1).MinSize(220, 200).CloseButton(False))

        center = wx.Panel(self)
        self.infobar = wx.InfoBar(center)
        self.book = wx.Simplebook(center)
        self.welcome = WelcomePanel(self.book, self.new_project, self.open_project_dialog)
        self.notebook = wx.Notebook(self.book)
        self.log_panel = LogPanel(self.notebook)
        self.notebook.AddPage(self.log_panel, "Log")
        self.book.AddPage(self.welcome, "welcome")
        self.book.AddPage(self.notebook, "project")
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.infobar, 0, wx.EXPAND)
        sizer.Add(self.book, 1, wx.EXPAND)
        center.SetSizer(sizer)
        self.aui.AddPane(center, aui.AuiPaneInfo().Name("center").CenterPane())

        self.timeline = TimelineController(self, self.infobar)
        self.aui.AddPane(self.timeline.panel, aui.AuiPaneInfo().Name("timeline").Caption("Timeline")
                         .Bottom().BestSize(-1, 210).MinSize(-1, 150).CloseButton(False))

        self._default_perspective = self.aui.SavePerspective()
        self.aui.Update()

    def _bind_events(self) -> None:
        self.Bind(wx.EVT_MENU, lambda _e: self.new_project(), id=wx.ID_NEW)
        self.Bind(wx.EVT_MENU, lambda _e: self.open_project_dialog(), id=wx.ID_OPEN)
        self.Bind(wx.EVT_MENU_RANGE, self._on_recent, id=wx.ID_FILE1, id2=wx.ID_FILE9)
        self.Bind(wx.EVT_MENU, self._on_show_folder, id=self.id_show_folder)
        self.Bind(wx.EVT_MENU, lambda _e: self.Close(), id=wx.ID_CLOSE)
        self.Bind(wx.EVT_MENU, self._on_quit, id=wx.ID_EXIT)
        self.Bind(wx.EVT_MENU, lambda _e: self.run_next(), id=self.id_run)
        self.Bind(wx.EVT_MENU, lambda _e: self.run_from(self._after_review()),
                  id=self.id_continue)
        self.Bind(wx.EVT_MENU, lambda _e: self.runner.cancel(), id=self.id_cancel)
        # The arrow part shows the dropdown menu natively; the button part pops it too.
        self.Bind(wx.EVT_TOOL, self._on_run_from_tool, id=self.id_run_from_tool)
        for item_id in self._run_from_ids:
            self.Bind(wx.EVT_MENU, self._on_run_from_menu, id=item_id)
        self.Bind(wx.EVT_MENU, self._on_reset_layout, id=self.id_reset_layout)
        self.Bind(wx.EVT_MENU, self._on_token, id=self.id_token)
        self.Bind(wx.EVT_MENU, self._on_about, id=wx.ID_ABOUT)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self.Bind(events.EVT_STAGE_STARTED, self._on_stage_started)
        self.Bind(events.EVT_PROGRESS, self._on_progress)
        self.Bind(events.EVT_LOG, lambda e: self.log_panel.append(e.level, e.msg))
        self.Bind(events.EVT_STAGE_DONE, self._on_stage_done)

    # --------------------------------------------------------------- projects
    def new_project(self, video: Path | None = None) -> None:
        with NewProjectDialog(self, video) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            try:
                project = Project.create(dlg.video_path(), dlg.work_dir_path(), dlg.overrides())
            except (ProjectError, OSError) as exc:
                wx.MessageBox(str(exc), "Could not create project", wx.OK | wx.ICON_ERROR, self)
                return
        self._show_project(project)

    def open_project_dialog(self) -> None:
        with wx.DirDialog(self, "Open a project work folder",
                          style=wx.DD_DIR_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.open_path(Path(dlg.GetPath()))

    def open_path(self, path: Path) -> None:
        """Open a project folder, or start a new project for a video file."""
        if path.is_dir() and is_project_dir(path):
            try:
                project = Project.open(path)
            except ProjectError as exc:
                wx.MessageBox(str(exc), "Could not open project", wx.OK | wx.ICON_ERROR, self)
                return
            self._show_project(project)
        elif path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            self.new_project(path)
        else:
            wx.MessageBox(f"{path} is neither a video nor a project folder.", "Open",
                          wx.OK | wx.ICON_WARNING, self)

    def _show_project(self, project: Project) -> None:
        target = self if self.project is None else self.app.window_for_project()
        target._attach(project)
        target.Raise()

    def _attach(self, project: Project) -> None:
        self.project = project
        self.SetTitle(f"{project.name} — ReVoiceOver")
        self.SetStatusText(str(project.work_dir), FIELD_PATH)
        self.app.history.AddFileToHistory(str(project.work_dir))
        self.app.save_history()
        self.log_panel.clear()
        self.book.SetSelection(PAGE_PROJECT)
        self.refresh_status()
        self.timeline.load(project.work_dir)

    # ------------------------------------------------------------------ runs
    def _first_pending(self) -> str | None:
        assert self.project is not None
        statuses = self.project.statuses()
        if self.project.awaiting_review(statuses):
            return None
        return next((n for n, s in statuses.items() if s is not stages.Status.DONE), None)

    @staticmethod
    def _after_review() -> str:
        return stages.STAGE_NAMES[stages.index(stages.REVIEW_AFTER) + 1]

    def run_next(self) -> None:
        if self.project is None:
            return
        start = self._first_pending()
        if start is None:
            if self.project.awaiting_review():
                self.infobar.ShowMessage("The script is waiting for review. Use Run → Approve "
                                         "Script & Continue when it is ready.", wx.ICON_INFORMATION)
            else:
                self.infobar.ShowMessage("Every stage is up to date.", wx.ICON_INFORMATION)
            return
        self._start(start=start)

    def run_from(self, stage: str) -> None:
        self._start(start=stage)

    def run_only(self, stage: str) -> None:
        self._start(start=stage, stop=stage)

    def _start(self, *, start: str | None, stop: str | None = None) -> None:
        if self.project is None or self.runner.running:
            return
        self.infobar.Dismiss()
        self.runner.start(self.project, start=start, stop=stop)
        self.SetStatusText("Running…", FIELD_MSG)
        self.gauge.SetValue(0)
        self.gauge.Show()
        self._update_ui()

    def _finished_from_worker(self, result: RunResult) -> None:
        wx.CallAfter(self._on_run_finished, result)

    def _on_run_finished(self, result: RunResult) -> None:
        if not self:  # window destroyed while the worker finished
            return
        self.gauge.Hide()
        self._current_stage = None
        self.refresh_status()
        messages = {
            Outcome.COMPLETED: ("Finished.", wx.ICON_INFORMATION),
            Outcome.REVIEW: ("The script is ready for review. When you are happy with it, "
                             "choose Run → Approve Script & Continue.", wx.ICON_INFORMATION),
            Outcome.CANCELLED: (f"Cancelled during {result.last_stage}.", wx.ICON_WARNING),
            Outcome.FAILED: (f"{result.last_stage} failed: {result.error}", wx.ICON_ERROR),
            Outcome.NOT_IMPLEMENTED: (f"{result.error}.", wx.ICON_INFORMATION),
        }
        text, icon = messages[result.outcome]
        self.SetStatusText(text.split(".")[0], FIELD_MSG)
        if result.outcome is not Outcome.COMPLETED:
            self.infobar.ShowMessage(text, icon)
        self._update_ui()

    def refresh_status(self) -> None:
        if self.project is None:
            return
        statuses = self.project.statuses()
        errors = {n: e for n in stages.STAGE_NAMES if (e := self.project.stage_error(n))}
        self.stages_panel.set_statuses(statuses, self.project.awaiting_review(statuses), errors)

    # --------------------------------------------------------- worker events
    def _on_stage_started(self, event: wx.PyEvent) -> None:
        self._current_stage = event.stage
        self.stages_panel.mark_running(event.stage)
        self.SetStatusText(f"Running {stages.get(event.stage).title.lower()}…", FIELD_MSG)
        self.gauge.Pulse()

    def _on_progress(self, event: wx.PyEvent) -> None:
        self.stages_panel.set_progress(event.stage, event.done, event.total, event.detail)
        if event.stage == self._current_stage and event.total:
            self.gauge.SetValue(min(100, round(100 * event.done / event.total)))

    def _on_stage_done(self, event: wx.PyEvent) -> None:
        self.refresh_status()
        if event.ok and event.stage in TIMELINE_STAGES and self.project is not None:
            self.timeline.load(self.project.work_dir)

    # ------------------------------------------------------------ UI updates
    def _update_ui(self) -> None:
        has_project = self.project is not None
        busy = self.runner.running
        for tool_id in (self.id_run, self.id_run_from_tool):
            self.toolbar.EnableTool(tool_id, has_project and not busy)
        self.toolbar.EnableTool(self.id_cancel, busy)
        bar = self.GetMenuBar()
        bar.Enable(self.id_run, has_project and not busy)
        bar.Enable(self.id_continue, has_project and not busy)
        bar.Enable(self.id_cancel, busy)
        bar.Enable(self.id_show_folder, has_project)
        for item_id in self._run_from_ids:
            bar.Enable(item_id, has_project and not busy)
        self.stages_panel.set_busy(busy)
        self.stages_panel.Enable(has_project)

    def _on_statusbar_size(self, event: wx.SizeEvent) -> None:
        rect = self.GetStatusBar().GetFieldRect(FIELD_GAUGE)
        self.gauge.SetRect(rect.Deflate(2, 2))
        event.Skip()

    # ------------------------------------------------------------- handlers
    def _on_run_from_menu(self, event: wx.CommandEvent) -> None:
        name = self._run_from_ids.get(event.GetId()) or self._run_from_ids_tb.get(event.GetId())
        if name:
            self.run_from(name)

    def _on_run_from_tool(self, _event: wx.CommandEvent) -> None:
        tool = self.toolbar.FindById(self.id_run_from_tool)
        self.toolbar.PopupMenu(tool.GetDropdownMenu())

    def _on_recent(self, event: wx.CommandEvent) -> None:
        path = Path(self.app.history.GetHistoryFile(event.GetId() - wx.ID_FILE1))
        if not is_project_dir(path):
            self.app.history.RemoveFileFromHistory(event.GetId() - wx.ID_FILE1)
            self.app.save_history()
            wx.MessageBox(f"{path} is no longer a project folder.", "Open Recent",
                          wx.OK | wx.ICON_WARNING, self)
            return
        self.open_path(path)

    def _on_show_folder(self, _event: wx.CommandEvent) -> None:
        if self.project is not None:
            wx.LaunchDefaultApplication(str(self.project.work_dir))

    def _on_reset_layout(self, _event: wx.CommandEvent) -> None:
        self.aui.LoadPerspective(self._default_perspective)

    def _on_token(self, _event: wx.CommandEvent) -> None:
        with TokenDialog(self) as dlg:
            dlg.ShowModal()

    def _on_about(self, _event: wx.CommandEvent) -> None:
        from narration import __version__

        info = wx.adv.AboutDialogInfo()
        info.SetName("ReVoiceOver")
        info.SetVersion(__version__)
        info.SetDescription("Rebuild a video's narration: transcribe, clean the script, "
                            "re-voice it in a cloned voice, and remux.")
        info.SetIcon(self.GetIcons().GetIcon(wx.Size(64, 64)))
        wx.adv.AboutBox(info, self)

    def _on_quit(self, _event: wx.CommandEvent) -> None:
        for frame in self.app.frames():
            if not frame.Close():
                return

    def _on_close(self, event: wx.CloseEvent) -> None:
        if self.runner.running:
            if event.CanVeto():
                answer = wx.MessageBox("A job is still running. Cancel it and close?",
                                       "Close Window", wx.YES_NO | wx.ICON_QUESTION, self)
                if answer != wx.YES:
                    event.Veto()
                    return
            self.runner.cancel()
            self.runner.join(timeout=10)
        self.timeline.stop()
        self._save_geometry()
        self.app.history.RemoveMenu(self.recent_menu)
        self.aui.UnInit()
        self.Destroy()

    # ------------------------------------------------------------ persistence
    def _restore_geometry(self) -> None:
        cfg = self.app.config
        perspective = cfg.Read("/Window/Perspective", "")
        if perspective and cfg.ReadInt("/Window/LayoutVersion", 0) == LAYOUT_VERSION:
            self.aui.LoadPerspective(perspective)
        w, h = cfg.ReadInt("/Window/Width", 0), cfg.ReadInt("/Window/Height", 0)
        if w and h:
            self.SetSize(wx.Size(w, h))
        if cfg.ReadBool("/Window/Maximized", False):
            self.Maximize()
        self.CentreOnScreen()

    def _save_geometry(self) -> None:
        cfg = self.app.config
        cfg.Write("/Window/Perspective", self.aui.SavePerspective())
        cfg.WriteInt("/Window/LayoutVersion", LAYOUT_VERSION)
        cfg.WriteBool("/Window/Maximized", self.IsMaximized())
        if not self.IsMaximized():
            w, h = self.GetSize()
            cfg.WriteInt("/Window/Width", w)
            cfg.WriteInt("/Window/Height", h)
        cfg.Flush()
