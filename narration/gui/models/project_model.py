"""A project = one source video + one work dir. No wx imports here."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from narration import artifacts, stages
from narration.config import CONFIG_NAME, Config, load_config, save_overrides
from narration.context import RunContext, default_work_dir
from narration.progress import ProgressReporter

PROJECT_FILE = "project.json"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")


class ProjectError(RuntimeError):
    pass


def is_project_dir(path: Path) -> bool:
    return (path / PROJECT_FILE).is_file()


@dataclass
class Project:
    video: Path
    work_dir: Path

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def create(cls, video: Path, work_dir: Path | None = None,
               overrides: dict[str, dict[str, Any]] | None = None) -> Project:
        """Create (or reopen) the project for ``video`` in ``work_dir``."""
        video = video.resolve()
        if not video.is_file():
            raise ProjectError(f"{video} does not exist")
        work_dir = (work_dir or default_work_dir(video)).resolve()
        if is_project_dir(work_dir):
            existing = cls.open(work_dir)
            if existing.video != video:
                raise ProjectError(
                    f"{work_dir} already belongs to {existing.video.name}; choose another folder"
                )
            project = existing
        else:
            work_dir.mkdir(parents=True, exist_ok=True)
            artifacts.write_json(work_dir / PROJECT_FILE, {"video": str(video)})
            project = cls(video, work_dir)
        if overrides:
            project.apply_overrides(overrides)
        return project

    @classmethod
    def open(cls, work_dir: Path) -> Project:
        work_dir = work_dir.resolve()
        try:
            data = artifacts.read_json(work_dir / PROJECT_FILE)
        except FileNotFoundError:
            raise ProjectError(f"{work_dir} is not a ReVoiceOver project") from None
        except (artifacts.SchemaError, json.JSONDecodeError) as exc:
            raise ProjectError(f"{work_dir / PROJECT_FILE}: {exc}") from None
        video = Path(data["video"])
        if not video.is_file():
            raise ProjectError(f"source video {video} is missing")
        return cls(video, work_dir)

    @property
    def name(self) -> str:
        return self.video.stem

    # -- config ------------------------------------------------------------
    def config(self) -> Config:
        return load_config(self.video, self.work_dir)

    def inherited_config(self) -> Config:
        """Config without the per-video layer — the base for overrides."""
        return load_config(self.video, None)

    def apply_overrides(self, overrides: dict[str, dict[str, Any]]) -> None:
        cfg = self.config()
        for section, values in overrides.items():
            sub = getattr(cfg, section)
            for key, value in values.items():
                setattr(sub, key, value)
        save_overrides(self.work_dir / CONFIG_NAME, cfg, self.inherited_config())

    # -- pipeline ----------------------------------------------------------
    def statuses(self) -> dict[str, stages.Status]:
        return stages.project_status(self.video, self.work_dir)

    def stage_error(self, name: str) -> str | None:
        return stages.stage_error(self.work_dir, name)

    def awaiting_review(self, statuses: dict[str, stages.Status] | None = None) -> bool:
        st = statuses or self.statuses()
        after = stages.STAGE_NAMES[stages.index(stages.REVIEW_AFTER) + 1]
        return st[stages.REVIEW_AFTER] is stages.Status.DONE and st[after] is not stages.Status.DONE

    def context(self, reporter: ProgressReporter, cancel: threading.Event) -> RunContext:
        return RunContext(video=self.video, work_dir=self.work_dir, config=self.config(),
                          reporter=reporter, cancel=cancel)
