"""Detail pane for one unit: raw text with deletions struck, editable clean text."""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import wx
import wx.stc as stc

from narration.gui import icons, theme
from narration.gui.models.timeline_model import format_time
from narration.types import Unit

IND_DELETED, IND_INSERTED, IND_GLOSSARY = 8, 9, 10
_TOKEN = re.compile(r"\S+")

FLAG_TEXT = {
    "dropped": "dropped (filler only)",
    "dropped-content": "dropped real words — check this",
    "short": "much shorter than spoken",
    "long": "longer than spoken — may not fit its slot",
}


def _key(word: str) -> str:
    return word.lower().strip(".,;:!?\"'“”‘’()…")


def diff_spans(raw: str, clean: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Char spans of words deleted from ``raw`` and inserted into ``clean``."""
    a = [(m.start(), m.end()) for m in _TOKEN.finditer(raw)]
    b = [(m.start(), m.end()) for m in _TOKEN.finditer(clean)]
    matcher = difflib.SequenceMatcher(
        a=[_key(raw[s:e]) for s, e in a], b=[_key(clean[s:e]) for s, e in b], autojunk=False)
    deleted, inserted = [], []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op in ("delete", "replace") and i2 > i1:
            deleted.append((a[i1][0], a[i2 - 1][1]))
        if op in ("insert", "replace") and j2 > j1:
            inserted.append((b[j1][0], b[j2 - 1][1]))
    return deleted, inserted


def glossary_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    spans = []
    for term in terms:
        for m in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", text):
            spans.append((m.start(), m.end()))
    return spans


@dataclass
class EditorCallbacks:
    commit: Callable[[str, str], None]          # unit_id, text
    play_original: Callable[[str], None]
    keep_original: Callable[[str, bool], None]
    revert_llm: Callable[[str], None]
    use_raw: Callable[[str], None]
    play_tts: Callable[[str], None]
    new_take: Callable[[str], None]
    next_unit: Callable[[], None]
    back_to_list: Callable[[], None]


def _style_editor(ed: stc.StyledTextCtrl, readonly: bool) -> None:
    pal = theme.palette()
    bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
    fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
    if readonly:
        bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE)
    font = wx.Font(wx.FontInfo(11))
    ed.StyleSetFont(stc.STC_STYLE_DEFAULT, font)
    ed.StyleSetBackground(stc.STC_STYLE_DEFAULT, bg)
    ed.StyleSetForeground(stc.STC_STYLE_DEFAULT, fg)
    ed.StyleClearAll()
    ed.SetCaretForeground(fg)
    ed.SetSelBackground(True, wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT))
    ed.SetSelForeground(True, wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHTTEXT))
    ed.SetWrapMode(stc.STC_WRAP_WORD)
    ed.SetMarginWidth(1, 0)
    ed.SetMarginWidth(0, 0)
    ed.SetMarginLeft(6)
    ed.SetUseHorizontalScrollBar(False)
    ed.SetReadOnly(readonly)
    ed.IndicatorSetStyle(IND_DELETED, stc.STC_INDIC_STRIKE)
    ed.IndicatorSetForeground(IND_DELETED, pal.playhead)
    ed.IndicatorSetStyle(IND_INSERTED, stc.STC_INDIC_PLAIN)
    ed.IndicatorSetForeground(IND_INSERTED, wx.Colour(46, 157, 91))
    ed.IndicatorSetStyle(IND_GLOSSARY, stc.STC_INDIC_ROUNDBOX)
    ed.IndicatorSetForeground(IND_GLOSSARY, pal.wave)
    ed.IndicatorSetAlpha(IND_GLOSSARY, 60)


def _paint(ed: stc.StyledTextCtrl, indicator: int, text: str,
           spans: list[tuple[int, int]]) -> None:
    ed.SetIndicatorCurrent(indicator)
    ed.IndicatorClearRange(0, ed.GetLength())
    for start, end in spans:  # STC positions are UTF-8 byte offsets
        b0 = len(text[:start].encode("utf-8"))
        b1 = len(text[:end].encode("utf-8"))
        ed.IndicatorFillRange(b0, b1 - b0)


class UnitEditor(wx.Panel):
    def __init__(self, parent: wx.Window, callbacks: EditorCallbacks) -> None:
        super().__init__(parent)
        self.cb = callbacks
        self.unit: Unit | None = None
        self.clip: dict[str, Any] | None = None  # synth.json entry for this unit
        self.busy = False
        self.glossary: list[str] = []
        self._loading = False

        self.header = wx.StaticText(self)
        self.header.SetFont(self.header.GetFont().Bold())
        self.flag_label = wx.StaticText(self)
        self.flag_label.SetForegroundColour(wx.Colour(217, 154, 30))

        self.raw = stc.StyledTextCtrl(self, size=wx.Size(-1, 60))
        _style_editor(self.raw, readonly=True)
        self.clean = stc.StyledTextCtrl(self, size=wx.Size(-1, 80))
        _style_editor(self.clean, readonly=False)

        self.play_btn = wx.Button(self, label="Play Original")
        self.play_btn.SetBitmap(icons.bundle("run"))
        self.play_btn.SetToolTip("Play this unit from the source recording (Space in the list)")
        self.tts_btn = wx.Button(self, label="Play Synthesized")
        self.tts_btn.SetBitmap(icons.bundle("run"))
        self.tts_btn.SetToolTip("Play the cloned-voice clip (Ctrl+Space in the list)")
        self.take_btn = wx.Button(self, label="New Take")
        self.take_btn.SetBitmap(icons.bundle("stale"))
        self.take_btn.SetToolTip("Synthesize this unit again from the current text "
                                 "(a different seed when the text is unchanged)")
        self.clip_info = wx.StaticText(self)
        self.keep_cb = wx.CheckBox(self, label="Keep my original voice for this unit")
        self.keep_cb.SetToolTip("Patch mode: use the original audio instead of re-synthesizing")
        self.revert_btn = wx.Button(self, label="Revert to LLM Text")
        self.raw_btn = wx.Button(self, label="Use Raw Text")
        self.ratio = wx.StaticText(self)
        hint = wx.StaticText(self, label="Ctrl+Enter: accept and next   Esc: back to list")
        hint.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(self.header, 0, wx.ALIGN_CENTER_VERTICAL)
        top.AddSpacer(12)
        top.Add(self.flag_label, 1, wx.ALIGN_CENTER_VERTICAL)
        top.Add(self.ratio, 0, wx.ALIGN_CENTER_VERTICAL)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.play_btn, 0, wx.RIGHT, 6)
        buttons.Add(self.tts_btn, 0, wx.RIGHT, 6)
        buttons.Add(self.take_btn, 0, wx.RIGHT, 10)
        buttons.Add(self.clip_info, 0, wx.ALIGN_CENTER_VERTICAL)
        buttons.AddStretchSpacer()
        buttons.Add(self.raw_btn, 0, wx.RIGHT, 6)
        buttons.Add(self.revert_btn)
        options = wx.BoxSizer(wx.HORIZONTAL)
        options.Add(self.keep_cb, 0, wx.ALIGN_CENTER_VERTICAL)

        def label(text: str) -> wx.StaticText:
            st = wx.StaticText(self, label=text)
            st.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            return st

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(top, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(label("As spoken (deleted words struck out)"), 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.raw, 1, wx.EXPAND | wx.ALL, 6)
        sizer.Add(label("Narration to synthesize"), 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.clean, 1, wx.EXPAND | wx.ALL, 6)
        sizer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        options.AddStretchSpacer()
        options.Add(hint, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(options, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

        self.clean.Bind(stc.EVT_STC_MODIFIED, self._on_modified)
        self.clean.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.clean.Bind(wx.EVT_KILL_FOCUS, self._on_kill_focus)
        self.play_btn.Bind(wx.EVT_BUTTON, lambda _e: self._with_unit(self.cb.play_original))
        self.keep_cb.Bind(wx.EVT_CHECKBOX, self._on_keep)
        self.revert_btn.Bind(wx.EVT_BUTTON, lambda _e: self._with_unit(self.cb.revert_llm))
        self.raw_btn.Bind(wx.EVT_BUTTON, lambda _e: self._with_unit(self.cb.use_raw))
        self.tts_btn.Bind(wx.EVT_BUTTON, lambda _e: self._with_unit(self.cb.play_tts))
        self.take_btn.Bind(wx.EVT_BUTTON, lambda _e: self._with_unit(self.cb.new_take))
        self.show(None)

    # -- display -----------------------------------------------------------
    def show(self, unit: Unit | None, clip: dict[str, Any] | None = None) -> None:
        self.unit = unit
        self.clip = clip
        self._loading = True
        try:
            self.raw.SetReadOnly(False)
            self.raw.SetText(unit.raw_text if unit else "")
            self.raw.SetReadOnly(True)
            self.clean.SetText((unit.clean_text or "") if unit else "")
            self.clean.EmptyUndoBuffer()
        finally:
            self._loading = False
        for ctrl in (self.clean, self.play_btn, self.keep_cb, self.revert_btn, self.raw_btn):
            ctrl.Enable(unit is not None)
        if unit is None:
            self.header.SetLabel("No unit selected")
            self.flag_label.SetLabel("")
            self.ratio.SetLabel("")
            self.clip_info.SetLabel("")
            self.tts_btn.Disable()
            self.take_btn.Disable()
            return
        self.header.SetLabel(f"{unit.id}   {format_time(unit.start, 0.1)} – "
                             f"{format_time(unit.end, 0.1)}   slot {unit.slot:.1f}s")
        self.keep_cb.SetValue(unit.keep_original)
        self.revert_btn.Enable(unit.edited)
        self._refresh_clip()
        self._refresh_marks()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._refresh_clip()

    def _refresh_clip(self) -> None:
        unit, clip = self.unit, self.clip
        has_text = bool(unit and unit.clean_text)
        self.tts_btn.Enable(clip is not None)
        self.take_btn.Enable(has_text and not self.busy)
        if unit is None or clip is None:
            self.clip_info.SetLabel("not synthesized yet" if has_text else "")
            self.clip_info.SetForegroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            return
        duration = float(clip["duration"])
        text = f"clip {duration:.1f}s / slot {unit.slot:.1f}s"
        colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT)
        if clip.get("text") and clip["text"] != unit.clean_text:
            text += " · text changed since synthesis"
            colour = wx.Colour(217, 154, 30)
        elif duration > unit.slot:
            text += f" · {duration / unit.slot:.0%} of slot"
            colour = wx.Colour(217, 154, 30)
        self.clip_info.SetLabel(text)
        self.clip_info.SetForegroundColour(colour)

    def _refresh_marks(self) -> None:
        if self.unit is None:
            return
        raw, clean = self.unit.raw_text, self.clean.GetText()
        deleted, inserted = diff_spans(raw, clean)
        _paint(self.raw, IND_DELETED, raw, deleted)
        _paint(self.clean, IND_INSERTED, clean, inserted)
        _paint(self.clean, IND_GLOSSARY, clean, glossary_spans(clean, self.glossary))
        ratio = len(clean.strip()) / max(1, len(raw))
        self.ratio.SetLabel(f"length {ratio:.0%} of spoken")
        flags = [FLAG_TEXT.get(f, f) for f in self.unit.flags]
        if self.unit.edited:
            flags.append("edited by you")
        self.flag_label.SetLabel("  ·  ".join(flags))
        self.Layout()

    # -- editing -----------------------------------------------------------
    @property
    def pending(self) -> bool:
        return self.unit is not None and \
            self.clean.GetText().strip() != (self.unit.clean_text or "")

    def commit(self) -> None:
        """Push the editor text into the model if it changed."""
        if self.pending and self.unit is not None:
            self.cb.commit(self.unit.id, self.clean.GetText())

    def focus_text(self) -> None:
        self.clean.SetFocus()
        self.clean.DocumentEnd()

    def _with_unit(self, fn: Callable[[str], None]) -> None:
        if self.unit is not None:
            self.commit()
            fn(self.unit.id)

    def _on_modified(self, event: stc.StyledTextEvent) -> None:
        if not self._loading and event.GetModificationType() & (
                stc.STC_MOD_INSERTTEXT | stc.STC_MOD_DELETETEXT):
            wx.CallAfter(self._refresh_marks)
        event.Skip()

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if event.ControlDown():
                self.commit()
                self.cb.next_unit()
            # Plain Enter would insert a newline; units are single paragraphs.
            return
        if key == wx.WXK_ESCAPE:
            self.commit()
            self.cb.back_to_list()
            return
        event.Skip()

    def _on_kill_focus(self, event: wx.FocusEvent) -> None:
        self.commit()
        event.Skip()

    def _on_keep(self, event: wx.CommandEvent) -> None:
        if self.unit is not None:
            self.cb.keep_original(self.unit.id, event.IsChecked())
