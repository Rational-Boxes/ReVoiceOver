"""Help viewer window and the small "?" buttons that open it at a topic."""

from __future__ import annotations

import re
from collections.abc import Callable

import wx
import wx.html

from narration.gui import help_content, icons

HELP_SIZE = wx.Size(980, 720)


def _hex(colour: wx.Colour) -> str:
    return colour.GetAsString(wx.C2S_HTML_SYNTAX)


def render(file: str) -> str:
    """Wrap a help fragment in a page coloured for the current (light/dark) theme."""
    sys = wx.SystemSettings
    bg = sys.GetColour(wx.SYS_COLOUR_WINDOW)
    fg = sys.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
    dark = sys.GetAppearance().IsDark()
    link = "#7fb2ff" if dark else "#1f62c4"
    note = "#2c3440" if dark else "#eaf2fd"
    pre = "#262a30" if dark else "#f2f3f5"
    body = help_content.source(file)
    body = body.replace('<table class="note">',
                        f'<table bgcolor="{note}" width="100%" cellpadding="10" border="0">')
    body = re.sub(r"<pre>(.*?)</pre>",
                  lambda m: f'<table bgcolor="{pre}" width="100%" cellpadding="8" border="0">'
                            f"<tr><td><pre>{m.group(1).strip(chr(10))}</pre></td></tr></table>",
                  body, flags=re.S)
    return (f'<html><body bgcolor="{_hex(bg)}" text="{_hex(fg)}" link="{link}">'
            f"{body}<br></body></html>")


class _HtmlView(wx.html.HtmlWindow):
    def __init__(self, parent: wx.Window, on_link: Callable[[str], None]) -> None:
        super().__init__(parent, style=wx.html.HW_SCROLLBAR_AUTO)
        self._on_link = on_link
        font = self.GetFont()
        self.SetStandardFonts(max(10, font.GetPointSize() + 1), font.GetFaceName(), "")

    def OnLinkClicked(self, link: wx.html.HtmlLinkInfo) -> None:  # noqa: N802 (wx API)
        self._on_link(link.GetHref())


class HelpFrame(wx.Frame):
    def __init__(self) -> None:
        super().__init__(None, title="ReVoiceOver Help", size=HELP_SIZE)
        self.SetIcons(icons.app_icon())
        self._history: list[tuple[str, str | None]] = []
        self._pos = -1
        self._tree_items: dict[tuple[str, str | None], wx.TreeItemId] = {}
        self._syncing = False

        tb = self.CreateToolBar(wx.TB_HORIZONTAL | wx.TB_FLAT)
        self.id_back, self.id_fwd, self.id_home = wx.NewIdRef(), wx.NewIdRef(), wx.NewIdRef()
        art = wx.ArtProvider
        tb.AddTool(self.id_back, "Back", art.GetBitmapBundle(wx.ART_GO_BACK, wx.ART_TOOLBAR),
                   "Back (Alt+Left)")
        tb.AddTool(self.id_fwd, "Forward",
                   art.GetBitmapBundle(wx.ART_GO_FORWARD, wx.ART_TOOLBAR), "Forward (Alt+Right)")
        tb.AddTool(self.id_home, "Contents", art.GetBitmapBundle(wx.ART_GO_HOME, wx.ART_TOOLBAR),
                   "Help contents")
        tb.AddStretchableSpace()
        self.search = wx.SearchCtrl(tb, size=wx.Size(240, -1))
        self.search.SetDescriptiveText("Search help")
        self.search.ShowCancelButton(True)
        tb.AddControl(self.search)
        tb.Realize()

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        self.side = wx.Simplebook(splitter)
        self.tree = wx.TreeCtrl(self.side, style=wx.TR_HIDE_ROOT | wx.TR_HAS_BUTTONS
                                | wx.TR_LINES_AT_ROOT | wx.TR_SINGLE)
        self.results = wx.ListBox(self.side)
        self.side.AddPage(self.tree, "contents")
        self.side.AddPage(self.results, "results")
        self.view = _HtmlView(splitter, self.follow)
        splitter.SplitVertically(self.side, self.view, 260)
        splitter.SetMinimumPaneSize(160)
        self._hits: list[help_content.Hit] = []
        self._fill_tree()

        self.Bind(wx.EVT_TOOL, lambda _e: self.go(-1), id=self.id_back)
        self.Bind(wx.EVT_TOOL, lambda _e: self.go(1), id=self.id_fwd)
        self.Bind(wx.EVT_TOOL, lambda _e: self.show("index.html"), id=self.id_home)
        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self._on_tree)
        self.search.Bind(wx.EVT_TEXT, self._on_search)
        self.search.Bind(wx.EVT_SEARCHCTRL_CANCEL_BTN, lambda _e: self.search.SetValue(""))
        self.results.Bind(wx.EVT_LISTBOX, self._on_result)
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_ALT, wx.WXK_LEFT, self.id_back),
            (wx.ACCEL_ALT, wx.WXK_RIGHT, self.id_fwd),
            (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, wx.ID_CLOSE),
        ]))
        self.Bind(wx.EVT_MENU, lambda _e: self.Close(), id=wx.ID_CLOSE)
        self.Bind(wx.EVT_SYS_COLOUR_CHANGED, lambda _e: self._render_current())

    # -- contents ----------------------------------------------------------
    def _fill_tree(self) -> None:
        root = self.tree.AddRoot("help")
        for topic in help_content.topics():
            item = self.tree.AppendItem(root, topic.title)
            self.tree.SetItemData(item, (topic.file, None))
            self._tree_items[(topic.file, None)] = item
            for section in topic.sections:
                sub = self.tree.AppendItem(item, section.title)
                self.tree.SetItemData(sub, (topic.file, section.anchor))
                self._tree_items[(topic.file, section.anchor)] = sub

    def _sync_tree(self, file: str, anchor: str | None) -> None:
        item = self._tree_items.get((file, anchor)) or self._tree_items.get((file, None))
        if item is not None and item.IsOk():
            self._syncing = True
            try:
                self.tree.SelectItem(item)
                self.tree.EnsureVisible(item)
            finally:
                self._syncing = False

    # -- navigation --------------------------------------------------------
    def show(self, file: str, anchor: str | None = None, record: bool = True) -> None:
        if help_content.topic(file) is None:
            file, anchor = "index.html", None
        if record:
            del self._history[self._pos + 1:]
            self._history.append((file, anchor))
            self._pos = len(self._history) - 1
        self._display(file, anchor)

    def _display(self, file: str, anchor: str | None) -> None:
        self.view.SetPage(render(file))
        if anchor:
            self.view.ScrollToAnchor(anchor)
        else:
            self.view.Scroll(0, 0)
        topic = help_content.topic(file)
        self.SetTitle(f"{topic.title if topic else 'Help'} — ReVoiceOver Help")
        self._sync_tree(file, anchor)
        tb = self.GetToolBar()
        tb.EnableTool(self.id_back, self._pos > 0)
        tb.EnableTool(self.id_fwd, self._pos < len(self._history) - 1)

    def _render_current(self) -> None:
        if 0 <= self._pos < len(self._history):
            self._display(*self._history[self._pos])

    def go(self, step: int) -> None:
        target = self._pos + step
        if 0 <= target < len(self._history):
            self._pos = target
            self._display(*self._history[target])

    def follow(self, href: str) -> None:
        """Handle a clicked link: other help pages, anchors, or the web."""
        if re.match(r"^[a-z]+://", href) or href.startswith("mailto:"):
            wx.LaunchDefaultBrowser(href)
            return
        if href.startswith("#"):
            current = self._history[self._pos][0] if self._history else "index.html"
            self.show(current, href[1:])
            return
        self.show(*help_content.parse_ref(href))

    # -- events ------------------------------------------------------------
    def _on_tree(self, event: wx.TreeEvent) -> None:
        if self._syncing:
            return
        data = self.tree.GetItemData(event.GetItem())
        if data:
            self.show(*data)

    def _on_search(self, _event: wx.CommandEvent) -> None:
        query = self.search.GetValue().strip()
        if not query:
            self.side.SetSelection(0)
            return
        self._hits = help_content.search(query)
        self.results.Set([h.title for h in self._hits] or ["No matches"])
        self.side.SetSelection(1)

    def _on_result(self, _event: wx.CommandEvent) -> None:
        i = self.results.GetSelection()
        if 0 <= i < len(self._hits):
            hit = self._hits[i]
            self.show(hit.file, hit.anchor)


def show_help(ref: str = "index") -> HelpFrame:
    """Open (or raise) the help window at ``ref`` such as "quality#pronunciations"."""
    app = wx.GetApp()
    frame: HelpFrame | None = getattr(app, "help_frame", None)
    if not frame:
        frame = HelpFrame()
        app.help_frame = frame
        frame.CentreOnScreen()
    frame.show(*help_content.parse_ref(ref))
    frame.Show()
    frame.Raise()
    return frame


def help_button(parent: wx.Window, ref: str, what: str = "") -> wx.BitmapButton:
    """A small "?" button that opens the help at ``ref``."""
    btn = wx.BitmapButton(parent, bitmap=icons.bundle("help"),
                          style=wx.BU_EXACTFIT | wx.BORDER_NONE)
    btn.SetToolTip(f"Help{': ' + what if what else ''}")
    btn.Bind(wx.EVT_BUTTON, lambda _e: show_help(ref))
    return btn
