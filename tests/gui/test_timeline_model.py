import numpy as np
import pytest

from narration.gui.models.timeline_model import TimelineModel, format_time
from narration.types import Unit


def U(i, start, end, slot_end):
    return Unit(id=f"u{i:04d}", start=start, end=end, slot_end=slot_end, raw_text=f"unit {i}")


@pytest.fixture
def model():
    units = [U(1, 1.0, 3.0, 4.0), U(2, 4.0, 6.0, 9.0), U(3, 9.0, 12.0, 100.0)]
    m = TimelineModel(duration=100.0, units=units, width=1000)
    m.set_units(units)
    return m


def test_geometry_round_trip(model):
    model.px_per_sec = 50
    model.scroll_to(10)
    assert model.x_for(12) == 100
    assert model.time_for(100) == 12


def test_scroll_clamped(model):
    model.px_per_sec = 50  # 20 s visible
    model.scroll_to(-5)
    assert model.offset == 0
    model.scroll_to(1000)
    assert model.offset == 80


def test_zoom_keeps_anchor(model):
    model.px_per_sec = 50
    model.scroll_to(20)
    t = model.time_for(300)
    model.zoom(2.0, anchor_x=300)
    assert model.px_per_sec == 100
    assert model.time_for(300) == pytest.approx(t)


def test_zoom_out_stops_at_fit(model):
    model.zoom(0.0001)
    assert model.px_per_sec == pytest.approx(10.0)  # 1000 px / 100 s
    model.zoom_to_fit()
    assert model.offset == 0 and model.visible_seconds == pytest.approx(100)


def test_unit_at_uses_slots(model):
    assert model.unit_at(0.5) is None
    assert model.unit_at(3.5).id == "u0001"  # silence after speech belongs to the slot
    assert model.unit_at(4.0).id == "u0002"
    assert model.unit_at(50).id == "u0003"


def test_neighbour_and_selection(model):
    assert model.neighbour(1).id == "u0001"
    model.selected = "u0002"
    assert model.neighbour(1).id == "u0003"
    assert model.neighbour(-1).id == "u0001"
    model.selected = "u0003"
    assert model.neighbour(1).id == "u0003"
    model.set_units(model.units[:1])
    assert model.selected is None


def test_ensure_visible(model):
    model.px_per_sec = 100  # 10 s visible
    model.ensure_visible(50, 52)
    assert model.offset <= 50 and 52 <= model.offset + model.visible_seconds


def test_visible_units(model):
    model.px_per_sec = 200  # 5 s
    model.scroll_to(3.5)
    assert [u.id for u in model.visible_units()] == ["u0001", "u0002"]


def test_waveform_columns_shape(model):
    model.peaks = np.ones((20000, 2), dtype=np.float32)
    cols = model.waveform_columns()
    assert cols.shape == (1000, 2)


def test_tick_step_and_format():
    m = TimelineModel(px_per_sec=10)
    assert m.tick_step() == 10
    assert format_time(75.25) == "1:15"
    assert format_time(75.25, 0.5) == "1:15.2"
