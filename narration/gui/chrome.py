"""Window chrome for MainFrame: menus, toolbar, About box, geometry persistence."""

from __future__ import annotations

from dataclasses import dataclass, field

import wx
import wx.adv
import wx.aui as aui

from narration import stages
from narration.gui import icons

LAYOUT_VERSION = 3  # bump when panes change so stale saved perspectives are ignored


@dataclass
class Ids:
    run: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    cancel: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    approve: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    show_folder: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    reset_layout: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    token: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    help_start: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    help_keys: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    run_from_tool: wx.WindowIDRef = field(default_factory=wx.NewIdRef)
    run_from: dict[int, str] = field(default_factory=dict)  # menu item id -> stage


def _stage_menu(ids: Ids) -> wx.Menu:
    menu = wx.Menu()
    for stage in stages.STAGES:
        item = menu.Append(wx.ID_ANY, stage.title)
        ids.run_from[item.GetId()] = stage.name
    return menu


def build_menubar(history: wx.FileHistory, ids: Ids) -> tuple[wx.MenuBar, wx.Menu]:
    """Returns the menubar and the recent-files submenu (to detach on close)."""
    bar = wx.MenuBar()

    file_menu = wx.Menu()
    file_menu.Append(wx.ID_NEW, "&New Project…\tCtrl+N")
    file_menu.Append(wx.ID_OPEN, "&Open Project…\tCtrl+O")
    recent = wx.Menu()
    file_menu.AppendSubMenu(recent, "Open &Recent")
    history.UseMenu(recent)
    history.AddFilesToMenu(recent)
    file_menu.AppendSeparator()
    file_menu.Append(wx.ID_SAVE, "&Save Script\tCtrl+S")
    file_menu.Append(ids.show_folder, "Show &Work Folder")
    file_menu.AppendSeparator()
    file_menu.Append(wx.ID_CLOSE, "&Close Window\tCtrl+W")
    file_menu.Append(wx.ID_EXIT, "&Quit\tCtrl+Q")
    bar.Append(file_menu, "&File")

    edit_menu = wx.Menu()
    edit_menu.Append(wx.ID_UNDO, "&Undo\tCtrl+Z")
    edit_menu.Append(wx.ID_REDO, "&Redo\tCtrl+Shift+Z")
    bar.Append(edit_menu, "&Edit")

    run_menu = wx.Menu()
    run_menu.Append(ids.run, "&Run\tCtrl+R", "Run from the first stage that is not done")
    run_menu.Append(ids.approve, "Approve Script && &Continue\tCtrl+Shift+R")
    run_menu.AppendSubMenu(_stage_menu(ids), "Run &From")
    run_menu.AppendSeparator()
    run_menu.Append(ids.cancel, "C&ancel\tCtrl+.")
    bar.Append(run_menu, "&Run")

    view_menu = wx.Menu()
    view_menu.Append(ids.reset_layout, "&Reset Layout")
    bar.Append(view_menu, "&View")

    settings_menu = wx.Menu()
    settings_menu.Append(ids.token, "DeepInfra &Token…")
    bar.Append(settings_menu, "&Settings")

    help_menu = wx.Menu()
    help_menu.Append(wx.ID_HELP, "ReVoiceOver &Help\tF1", "Help for the current tab")
    help_menu.Append(ids.help_start, "&Getting Started")
    help_menu.Append(ids.help_keys, "&Keyboard Shortcuts")
    help_menu.AppendSeparator()
    help_menu.Append(wx.ID_ABOUT, "&About ReVoiceOver")
    bar.Append(help_menu, "&Help")
    return bar, recent


def build_toolbar(frame: wx.Frame, ids: Ids) -> wx.ToolBar:
    tb = frame.CreateToolBar(wx.TB_HORIZONTAL | wx.TB_TEXT | wx.TB_HORZ_LAYOUT | wx.TB_FLAT)
    tb.SetToolBitmapSize(icons.TOOLBAR)
    tb.AddTool(wx.ID_NEW, "New", icons.bundle("new", icons.TOOLBAR), "New project")
    tb.AddTool(wx.ID_OPEN, "Open", icons.bundle("open", icons.TOOLBAR), "Open project")
    tb.AddSeparator()
    tb.AddTool(ids.run, "Run", icons.bundle("run", icons.TOOLBAR),
               "Run from the first stage that is not done")
    tb.AddTool(ids.run_from_tool, "Run From", icons.bundle("run-from", icons.TOOLBAR),
               "Run from a chosen stage", wx.ITEM_DROPDOWN)
    tb.SetDropdownMenu(ids.run_from_tool, _stage_menu(ids))
    tb.AddTool(ids.cancel, "Cancel", icons.bundle("cancel", icons.TOOLBAR),
               "Cancel the running job")
    tb.Realize()
    return tb


def show_about(parent: wx.Window) -> None:
    from narration import LICENSE_NOTICE, __copyright__, __version__

    info = wx.adv.AboutDialogInfo()
    info.SetName("ReVoiceOver")
    info.SetVersion(__version__)
    info.SetDescription("Rebuild a video's narration: transcribe, clean the script, "
                        "re-voice it in a cloned voice, and remux.")
    info.SetIcon(icons.app_icon().GetIcon(wx.Size(64, 64)))
    info.SetCopyright(__copyright__)
    info.SetLicence(LICENSE_NOTICE)
    info.SetWebSite("https://github.com/Rational-Boxes/ReVoiceOver")
    wx.adv.AboutBox(info, parent)


def restore_geometry(frame: wx.Frame, mgr: aui.AuiManager, cfg: wx.ConfigBase) -> None:
    perspective = cfg.Read("/Window/Perspective", "")
    if perspective and cfg.ReadInt("/Window/LayoutVersion", 0) == LAYOUT_VERSION:
        mgr.LoadPerspective(perspective)
    w, h = cfg.ReadInt("/Window/Width", 0), cfg.ReadInt("/Window/Height", 0)
    if w and h:
        frame.SetSize(wx.Size(w, h))
    if cfg.ReadBool("/Window/Maximized", False):
        frame.Maximize()
    frame.CentreOnScreen()


def save_geometry(frame: wx.Frame, mgr: aui.AuiManager, cfg: wx.ConfigBase) -> None:
    cfg.Write("/Window/Perspective", mgr.SavePerspective())
    cfg.WriteInt("/Window/LayoutVersion", LAYOUT_VERSION)
    cfg.WriteBool("/Window/Maximized", frame.IsMaximized())
    if not frame.IsMaximized():
        w, h = frame.GetSize()
        cfg.WriteInt("/Window/Width", w)
        cfg.WriteInt("/Window/Height", h)
    cfg.Flush()
