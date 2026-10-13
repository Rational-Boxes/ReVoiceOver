"""The script under review: units, edits, filters, undo. No wx imports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from narration import artifacts
from narration.stages.clean import flags_for, normalize, script_markdown
from narration.types import Unit

SCRIPT = "script.json"


@dataclass(frozen=True)
class Change:
    unit_id: str
    field: str
    old: Any
    new: Any


class ScriptModel:
    def __init__(self) -> None:
        self.units: list[Unit] = []
        self.meta: dict[str, Any] = {}
        self.work_dir: Path | None = None
        self.video_name = ""
        self.dirty = False
        self._undo: list[Change] = []
        self._redo: list[Change] = []
        self._index: dict[str, int] = {}

    # -- load / save -------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self.work_dir is not None and bool(self.units)

    def load(self, work_dir: Path, video_name: str) -> bool:
        """Load script.json; returns False (and clears) when there is none."""
        self.work_dir, self.video_name = work_dir, video_name
        try:
            data = artifacts.read_json(work_dir / SCRIPT)
        except (FileNotFoundError, artifacts.SchemaError):
            data = {"units": []}
        self.meta = {k: v for k, v in data.items() if k != "units"}
        self.units = [Unit.from_dict(u) for u in data["units"]]
        self._index = {u.id: i for i, u in enumerate(self.units)}
        self.dirty = False
        self._undo.clear()
        self._redo.clear()
        return bool(self.units)

    def save(self) -> None:
        if self.work_dir is None:
            return
        artifacts.write_json(self.work_dir / SCRIPT,
                             {**self.meta, "units": [u.to_dict() for u in self.units]})
        artifacts.atomic_write_text(self.work_dir / "script.md",
                                    script_markdown(self.units, self.video_name))
        self.dirty = False

    # -- access ------------------------------------------------------------
    def get(self, unit_id: str) -> Unit | None:
        i = self._index.get(unit_id)
        return self.units[i] if i is not None else None

    def index_of(self, unit_id: str) -> int | None:
        return self._index.get(unit_id)

    def filtered(self, query: str = "", flagged_only: bool = False,
                 changed_only: bool = False) -> list[Unit]:
        q = query.strip().lower()
        out = []
        for u in self.units:
            if flagged_only and not u.flags:
                continue
            if changed_only and normalize(u.clean_text or "") == normalize(u.raw_text):
                continue
            if q and q not in u.raw_text.lower() and q not in (u.clean_text or "").lower() \
                    and q != u.id:
                continue
            out.append(u)
        return out

    def stats(self) -> dict[str, int]:
        return {
            "units": len(self.units),
            "flagged": sum(1 for u in self.units if u.flags),
            "edited": sum(1 for u in self.units if u.edited),
            "changed": sum(1 for u in self.units
                           if normalize(u.clean_text or "") != normalize(u.raw_text)),
        }

    def tts_characters(self) -> int:
        return sum(len(u.clean_text or "") for u in self.units if not u.keep_original)

    # -- edits -------------------------------------------------------------
    def _apply(self, unit: Unit, field: str, value: Any) -> None:
        setattr(unit, field, value)
        if field == "clean_text":
            unit.edited = (value or "") != (unit.llm_text or "")
            unit.flags = flags_for(unit.raw_text, value or "")
        self.dirty = True

    def set(self, unit_id: str, field: str, value: Any) -> bool:
        """Change a field (clean_text or keep_original); returns True if it changed."""
        unit = self.get(unit_id)
        if unit is None or getattr(unit, field) == value:
            return False
        self._undo.append(Change(unit_id, field, getattr(unit, field), value))
        self._redo.clear()
        self._apply(unit, field, value)
        return True

    def set_text(self, unit_id: str, text: str) -> bool:
        return self.set(unit_id, "clean_text", text.strip())

    def revert_to_llm(self, unit_id: str) -> bool:
        unit = self.get(unit_id)
        return unit is not None and self.set_text(unit_id, unit.llm_text or "")

    def revert_to_raw(self, unit_id: str) -> bool:
        unit = self.get(unit_id)
        return unit is not None and self.set_text(unit_id, unit.raw_text)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> str | None:
        """Undo the last change; returns the affected unit id."""
        if not self._undo:
            return None
        change = self._undo.pop()
        self._redo.append(change)
        unit = self.get(change.unit_id)
        if unit is not None:
            self._apply(unit, change.field, change.old)
        return change.unit_id

    def redo(self) -> str | None:
        if not self._redo:
            return None
        change = self._redo.pop()
        self._undo.append(change)
        unit = self.get(change.unit_id)
        if unit is not None:
            self._apply(unit, change.field, change.new)
        return change.unit_id
