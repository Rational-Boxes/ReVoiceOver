"""Timeline: ruler, source waveform and narration-unit blocks, with zoom and playhead."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import wx

from narration.gui import icons, theme
from narration.gui.models.timeline_model import TimelineModel, format_time
from narration.types import Unit

RULER_H = 22
UNITS_H = 30
PAD = 4
DRAG_THRESHOLD = 4
ZOOM_STEP = 1.25


class TimelineCanvas(wx.Window):
    def __init__(self, parent: wx.Window, model: TimelineModel,
                 on_select: Callable[[str], None], on_activate: Callable[[str], None]) -> None:
        super().__init__(parent, style=wx.WANTS_CHARS | wx.FULL_REPAINT_ON_RESIZE)
        self.model = model
        self.on_select = on_select
        self.on_activate = on_activate
        self.on_view_changed: Callable[[], None] = lambda: None
        self.playhead: float | None = None
        self._drag_from: tuple[int, float] | None = None
        self._dragged = False
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize(wx.Size(-1, RULER_H + UNITS_H + 70))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_LEFT_DCLICK, self._on_dclick)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.Bind(wx.EVT_SYS_COLOUR_CHANGED, lambda _e: self.Refresh())

    # -- layout ------------------------------------------------------------
    def _lanes(self) -> tuple[wx.Rect, wx.Rect, wx.Rect]:
        w, h = self.GetClientSize()
        ruler = wx.Rect(0, 0, w, RULER_H)
        units = wx.Rect(0, h - UNITS_H, w, UNITS_H)
        wave = wx.Rect(0, RULER_H, w, max(10, h - RULER_H - UNITS_H))
        return ruler, wave, units

    def changed(self) -> None:
        self.Refresh()
        self.on_view_changed()

    # -- painting ----------------------------------------------------------
    def _on_paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        pal = theme.palette()
        dc.SetBackground(wx.Brush(pal.background))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        ruler, wave, units = self._lanes()
        m = self.model

        gc.SetPen(wx.NullPen)
        gc.SetBrush(wx.Brush(pal.lane))
        gc.DrawRectangle(wave.x, wave.y, wave.width, wave.height)
        self._draw_ruler(gc, dc, ruler, wave, pal)
        self._draw_selection_band(gc, wave, pal)
        self._draw_waveform(gc, wave, pal)
        self._draw_units(gc, units, pal)
        if self.playhead is not None:
            x = m.x_for(self.playhead)
            gc.SetPen(wx.Pen(pal.playhead, 2))
            gc.StrokeLine(x, 0, x, units.bottom)

        if not m.units and m.peaks.size == 0:
            gc.SetFont(self.GetFont(), pal.ruler_text)
            msg = "Run “Extract audio” to see the waveform here."
            tw, th = gc.GetTextExtent(msg)
            gc.DrawText(msg, (wave.width - tw) / 2, wave.y + (wave.height - th) / 2)

    def _draw_ruler(self, gc: wx.GraphicsContext, dc: wx.DC, ruler: wx.Rect, wave: wx.Rect,
                    pal: theme.Palette) -> None:
        m = self.model
        step = m.tick_step()
        font = self.GetFont().Smaller()
        gc.SetFont(font, pal.ruler_text)
        first = int(m.offset // step) * step
        t = first
        end = m.offset + m.visible_seconds
        while t <= end:
            x = m.x_for(t)
            gc.SetPen(wx.Pen(pal.grid, 1))
            gc.StrokeLine(x, ruler.bottom - 6, x, wave.bottom)
            gc.DrawText(format_time(t, step), x + 3, ruler.y + 3)
            t += step

    def _draw_selection_band(self, gc: wx.GraphicsContext, wave: wx.Rect,
                             pal: theme.Palette) -> None:
        unit = self.model.selected_unit()
        if unit is None:
            return
        x0, x1 = self.model.x_for(unit.start), self.model.x_for(unit.slot_end)
        gc.SetPen(wx.NullPen)
        gc.SetBrush(wx.Brush(pal.selected_band))
        gc.DrawRectangle(x0, wave.y, x1 - x0, wave.height)

    def _draw_waveform(self, gc: wx.GraphicsContext, wave: wx.Rect, pal: theme.Palette) -> None:
        cols = self.model.waveform_columns()
        if not cols.any():
            return
        mid = wave.y + wave.height / 2
        scale = (wave.height / 2 - PAD) / max(1e-6, float(np.abs(self.model.peaks).max()))
        xs = np.arange(len(cols), dtype=np.float64)
        tops = mid - np.maximum(cols[:, 1], 0) * scale - 0.5
        bottoms = mid - np.minimum(cols[:, 0], 0) * scale + 0.5
        path = gc.CreatePath()
        path.MoveToPoint(0, float(tops[0]))
        for x, y in zip(xs[1:], tops[1:], strict=True):
            path.AddLineToPoint(float(x), float(y))
        for x, y in zip(xs[::-1], bottoms[::-1], strict=True):
            path.AddLineToPoint(float(x), float(y))
        path.CloseSubpath()
        gc.SetPen(wx.NullPen)
        gc.SetBrush(wx.Brush(pal.wave))
        gc.FillPath(path)

    def _draw_units(self, gc: wx.GraphicsContext, lane: wx.Rect, pal: theme.Palette) -> None:
        m = self.model
        font = self.GetFont().Smaller()
        top, height = lane.y + 3, lane.height - 6
        for unit in m.visible_units():
            x0, x1, xs = m.x_for(unit.start), m.x_for(unit.end), m.x_for(unit.slot_end)
            gc.SetPen(wx.NullPen)
            gc.SetBrush(wx.Brush(pal.slot))
            gc.DrawRectangle(x1, top + height * 0.3, max(0.0, xs - x1 - 1), height * 0.4)
            gc.SetBrush(wx.Brush(pal.unit))
            gc.DrawRoundedRectangle(x0, top, max(2.0, x1 - x0), height, 3)
            if unit.id == m.selected:
                gc.SetPen(wx.Pen(pal.selected, 2))
                gc.SetBrush(wx.TRANSPARENT_BRUSH)
                gc.DrawRoundedRectangle(x0 - 1, top - 1, max(2.0, x1 - x0) + 2, height + 2, 3)
            label_w = x1 - x0 - 8
            if label_w > 24:
                gc.SetFont(font, pal.unit_text)
                gc.Clip(x0 + 4, top, label_w, height)
                text = unit.clean_text or unit.raw_text
                th = gc.GetTextExtent(text)[1]
                gc.DrawText(text, x0 + 4, top + (height - th) / 2)
                gc.ResetClip()

    # -- interaction -------------------------------------------------------
    def _on_size(self, event: wx.SizeEvent) -> None:
        self.model.set_width(self.GetClientSize().width)
        self.changed()
        event.Skip()

    def _on_wheel(self, event: wx.MouseEvent) -> None:
        rotation = event.GetWheelRotation() / max(1, event.GetWheelDelta())
        if event.ControlDown():
            self.model.zoom(ZOOM_STEP ** rotation, event.GetX())
        else:
            horizontal = event.GetWheelAxis() == wx.MOUSE_WHEEL_HORIZONTAL
            self.model.scroll_by_px((rotation if horizontal else -rotation) * 60)
        self.changed()

    def _on_left_down(self, event: wx.MouseEvent) -> None:
        self.SetFocus()
        self._drag_from = (event.GetX(), self.model.offset)
        self._dragged = False
        self.CaptureMouse()

    def _on_motion(self, event: wx.MouseEvent) -> None:
        if self._drag_from is None or not event.LeftIsDown():
            return
        x0, offset0 = self._drag_from
        if abs(event.GetX() - x0) >= DRAG_THRESHOLD:
            self._dragged = True
            self.SetCursor(wx.Cursor(wx.CURSOR_SIZEWE))
        if self._dragged:
            self.model.scroll_to(offset0 - (event.GetX() - x0) / self.model.px_per_sec)
            self.changed()

    def _on_left_up(self, event: wx.MouseEvent) -> None:
        if self.HasCapture():
            self.ReleaseMouse()
        self.SetCursor(wx.NullCursor)
        if self._drag_from is not None and not self._dragged:
            unit = self.model.unit_at(self.model.time_for(event.GetX()))
            if unit is not None:
                self.on_select(unit.id)
        self._drag_from = None

    def _on_dclick(self, event: wx.MouseEvent) -> None:
        unit = self.model.unit_at(self.model.time_for(event.GetX()))
        if unit is not None:
            self.on_select(unit.id)
            self.on_activate(unit.id)

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        m = self.model
        if key in (wx.WXK_LEFT, wx.WXK_RIGHT):
            unit = m.neighbour(-1 if key == wx.WXK_LEFT else 1)
            if unit is not None:
                self.on_select(unit.id)
        elif key == wx.WXK_SPACE and m.selected:
            self.on_activate(m.selected)
        elif key in (ord("+"), ord("="), wx.WXK_NUMPAD_ADD):
            m.zoom(ZOOM_STEP)
            self.changed()
        elif key in (ord("-"), wx.WXK_NUMPAD_SUBTRACT):
            m.zoom(1 / ZOOM_STEP)
            self.changed()
        elif key == wx.WXK_HOME:
            m.scroll_to(0)
            self.changed()
        elif key == wx.WXK_END:
            m.scroll_to(m.max_offset)
            self.changed()
        else:
            event.Skip()


class TimelinePanel(wx.Panel):
    """Canvas + scrollbar + a small control strip."""

    def __init__(self, parent: wx.Window, on_select: Callable[[str], None],
                 on_activate: Callable[[str], None], on_stop: Callable[[], None]) -> None:
        super().__init__(parent)
        self.model = TimelineModel()
        self.canvas = TimelineCanvas(self, self.model, on_select, on_activate)
        self.canvas.on_view_changed = self._sync_scrollbar
        self.scrollbar = wx.ScrollBar(self, style=wx.SB_HORIZONTAL)

        self.play_btn = wx.BitmapButton(self, bitmap=icons.bundle("run"))
        self.play_btn.SetToolTip("Play the selected unit's original audio (Space)")
        self.stop_btn = wx.BitmapButton(self, bitmap=icons.bundle("cancel"))
        self.stop_btn.SetToolTip("Stop playback")
        fit_btn = wx.Button(self, label="Fit", style=wx.BU_EXACTFIT)
        fit_btn.SetToolTip("Show the whole recording")
        zoom_in = wx.Button(self, label="+", style=wx.BU_EXACTFIT)
        zoom_out = wx.Button(self, label="−", style=wx.BU_EXACTFIT)
        zoom_in.SetToolTip("Zoom in (Ctrl+wheel)")
        zoom_out.SetToolTip("Zoom out (Ctrl+wheel)")
        self.info = wx.StaticText(self, style=wx.ST_ELLIPSIZE_END)

        strip = wx.BoxSizer(wx.HORIZONTAL)
        for ctrl in (self.play_btn, self.stop_btn):
            strip.Add(ctrl, 0, wx.RIGHT, 2)
        strip.AddSpacer(8)
        for ctrl in (zoom_out, zoom_in, fit_btn):
            strip.Add(ctrl, 0, wx.RIGHT, 2)
        strip.AddSpacer(10)
        strip.Add(self.info, 1, wx.ALIGN_CENTER_VERTICAL)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(strip, 0, wx.EXPAND | wx.ALL, 3)
        sizer.Add(self.canvas, 1, wx.EXPAND)
        sizer.Add(self.scrollbar, 0, wx.EXPAND)
        self.SetSizer(sizer)

        self._on_activate = on_activate
        self.play_btn.Bind(wx.EVT_BUTTON, self._on_play)
        self.stop_btn.Bind(wx.EVT_BUTTON, lambda _e: on_stop())
        fit_btn.Bind(wx.EVT_BUTTON, lambda _e: self._zoom(None))
        zoom_in.Bind(wx.EVT_BUTTON, lambda _e: self._zoom(ZOOM_STEP))
        zoom_out.Bind(wx.EVT_BUTTON, lambda _e: self._zoom(1 / ZOOM_STEP))
        self.scrollbar.Bind(wx.EVT_SCROLL, self._on_scroll)
        self.set_selected_info(None)

    def _on_play(self, _event: wx.CommandEvent) -> None:
        if self.model.selected:
            self._on_activate(self.model.selected)

    # -- data --------------------------------------------------------------
    def set_data(self, duration: float, peaks: np.ndarray, units: list[Unit]) -> None:
        fresh = self.model.duration != duration
        self.model.duration = duration
        self.model.peaks = peaks
        self.model.set_units(units)
        if fresh:
            self.model.zoom_to_fit()
        self.canvas.changed()
        self.set_selected_info(self.model.selected_unit())

    def select(self, unit_id: str | None) -> None:
        self.model.selected = unit_id
        unit = self.model.selected_unit()
        if unit is not None:
            self.model.ensure_visible(unit.start, unit.slot_end)
        self.set_selected_info(unit)
        self.canvas.changed()

    def set_selected_info(self, unit: Unit | None) -> None:
        if unit is None:
            n = len(self.model.units)
            self.info.SetLabel(f"{n} units — click one to select, double-click to play"
                               if n else "")
        else:
            self.info.SetLabel(f"{unit.id}  {format_time(unit.start, 0.1)}–"
                               f"{format_time(unit.end, 0.1)}  (slot {unit.slot:.1f}s)  "
                               f"{unit.clean_text or unit.raw_text}")
        self.play_btn.Enable(unit is not None)

    def set_playhead(self, t: float | None) -> None:
        if t != self.canvas.playhead:
            self.canvas.playhead = t
            if t is not None and not (self.model.offset <= t
                                      <= self.model.offset + self.model.visible_seconds):
                self.model.scroll_to(t - self.model.visible_seconds * 0.1)
                self._sync_scrollbar()
            self.canvas.Refresh()

    # -- view --------------------------------------------------------------
    def _zoom(self, factor: float | None) -> None:
        if factor is None:
            self.model.zoom_to_fit()
        else:
            self.model.zoom(factor)
        self.canvas.changed()

    def _sync_scrollbar(self) -> None:
        m = self.model
        total = max(1, int(m.duration * m.px_per_sec))
        self.scrollbar.SetScrollbar(int(m.offset * m.px_per_sec), m.width, total,
                                    max(1, m.width - 40))

    def _on_scroll(self, event: wx.ScrollEvent) -> None:
        self.model.scroll_to(event.GetPosition() / self.model.px_per_sec)
        self.canvas.Refresh()
