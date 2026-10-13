from narration import artifacts
from narration.gui.models.script_model import ScriptModel
from narration.types import Unit


def make_script(work, units):
    artifacts.write_json(work / "script.json",
                         {"model": "m", "prompt_sha": "x", "units": [u.to_dict() for u in units]})


def U(i, raw, clean, flags=()):
    return Unit(id=f"u{i:04d}", start=i, end=i + 0.8, slot_end=i + 1.0, raw_text=raw,
                clean_text=clean, llm_text=clean, flags=list(flags))


def test_load_edit_save_round_trip(tmp_path):
    make_script(tmp_path, [U(1, "um open it", "Open it."), U(2, "uh workspace wide.", "",
                                                             ["dropped-content"])])
    m = ScriptModel()
    assert m.load(tmp_path, "v.mp4")
    assert m.stats() == {"units": 2, "flagged": 1, "edited": 0, "changed": 2}

    assert m.set_text("u0002", "  Workspace wide.  ")
    u = m.get("u0002")
    assert u.clean_text == "Workspace wide." and u.edited and u.flags == []
    assert m.dirty
    assert not m.set_text("u0002", "Workspace wide.")  # no-op change

    m.save()
    assert not m.dirty
    data = artifacts.read_json(tmp_path / "script.json")
    assert data["model"] == "m"
    assert data["units"][1]["clean_text"] == "Workspace wide."
    assert data["units"][1]["edited"] is True
    assert "Workspace wide." in (tmp_path / "script.md").read_text()


def test_undo_redo_and_revert(tmp_path):
    make_script(tmp_path, [U(1, "um open it", "Open it.")])
    m = ScriptModel()
    m.load(tmp_path, "v")
    m.set_text("u0001", "Open the thing.")
    m.set("u0001", "keep_original", True)
    assert m.undo() == "u0001" and m.get("u0001").keep_original is False
    assert m.undo() == "u0001" and m.get("u0001").clean_text == "Open it."
    assert not m.get("u0001").edited
    assert m.undo() is None
    assert m.redo() == "u0001" and m.get("u0001").clean_text == "Open the thing."
    assert m.revert_to_llm("u0001") and m.get("u0001").clean_text == "Open it."
    assert m.revert_to_raw("u0001") and m.get("u0001").clean_text == "um open it"
    assert not m.can_redo


def test_filters(tmp_path):
    make_script(tmp_path, [U(1, "Open it.", "Open it."), U(2, "um close it", "Close it.",
                                                         ["short"]), U(3, "Save.", "Save.")])
    m = ScriptModel()
    m.load(tmp_path, "v")
    assert [u.id for u in m.filtered(flagged_only=True)] == ["u0002"]
    assert [u.id for u in m.filtered(changed_only=True)] == ["u0002"]
    assert [u.id for u in m.filtered("save")] == ["u0003"]
    assert [u.id for u in m.filtered("u0001")] == ["u0001"]
    assert m.tts_characters() == len("Open it.") + len("Close it.") + len("Save.")


def test_missing_script(tmp_path):
    m = ScriptModel()
    assert not m.load(tmp_path, "v") and not m.loaded
