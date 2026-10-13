"""The script under review: units, edits, filters, undo. No wx imports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from narration import artifacts
from narration.stages.clean import flags_for, normalize, script_markdown
from narration.types import Unit

SCRIPT = "script.json"
QA_FILE = "qa.json"


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
        self.qa: dict[str, dict[str, Any]] = {}  # qa.json group result, by unit id

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
        self.load_qa()
        return bool(self.units)

    # -- QA ----------------------------------------------------------------
    def load_qa(self) -> None:
        self.qa = {}
        if self.work_dir is None:
            return
        try:
            groups = artifacts.read_json(self.work_dir / QA_FILE)["groups"]
        except (FileNotFoundError, artifacts.SchemaError, KeyError):
            return
        for gid, entry in groups.items():
            for uid in entry.get("units", []):
                self.qa[uid] = {**entry, "group": gid}

    def qa_flags(self, unit_id: str) -> list[str]:
        """Open QA problems for a unit (none once accepted)."""
        entry = self.qa.get(unit_id)
        if not entry or entry.get("accepted"):
            return []
        return [f"qa-{f}" for f in entry.get("flags", [])]

    def all_flags(self, unit: Unit) -> list[str]:
        return [*unit.flags, *self.qa_flags(unit.id)]

    def qa_failures(self) -> list[Unit]:
        return [u for u in self.units if self.qa_flags(u.id)]

    def qa_failing_groups(self) -> list[tuple[str, dict[str, Any]]]:
        """(group id, result) for each speech group with open QA problems, in order."""
        seen: dict[str, dict[str, Any]] = {}
        for u in self.qa_failures():
            entry = self.qa[u.id]
            seen.setdefault(entry["group"], entry)
        return list(seen.items())

    def qa_checked(self) -> int:
        return len({e["group"] for e in self.qa.values()})

    def accept_qa(self, unit_id: str) -> None:
        """Accept the QA problem of a unit's speech group (written to qa.json right away)."""
        if self.work_dir is None or unit_id not in self.qa:
            return
        gid = self.qa[unit_id]["group"]
        data = artifacts.read_json(self.work_dir / QA_FILE)
        data["groups"][gid]["accepted"] = True
        artifacts.write_json(self.work_dir / QA_FILE, data)
        for entry in self.qa.values():
            if entry["group"] == gid:
                entry["accepted"] = True

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
            if flagged_only and not self.all_flags(u):
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
            "flagged": sum(1 for u in self.units if self.all_flags(u)),
            "edited": sum(1 for u in self.units if u.edited),
            "changed": sum(1 for u in self.units
                           if normalize(u.clean_text or "") != normalize(u.raw_text)),
        }

    def tts_characters(self) -> int:
        return sum(len(u.clean_text or "") for u in self.units)

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
