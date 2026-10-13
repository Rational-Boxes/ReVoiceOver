"""RunContext: everything a stage needs — paths, config, reporter, cancellation."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from narration.config import Config
from narration.credentials import mask
from narration.progress import NullReporter, ProgressReporter


class Cancelled(Exception):
    """Raised when the user cancels a run; partial per-unit outputs are kept."""


def default_work_dir(video: Path, base: Path | None = None) -> Path:
    """``<base>/work/<video-stem>``; ``base`` defaults to the video's folder."""
    base = base if base is not None else video.resolve().parent
    return base / "work" / video.stem


@dataclass
class RunContext:
    video: Path
    work_dir: Path
    config: Config
    reporter: ProgressReporter = field(default_factory=NullReporter)
    cancel: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.video = self.video.resolve()
        self.work_dir = self.work_dir.resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------
    def path(self, *parts: str) -> Path:
        return self.work_dir.joinpath(*parts)

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.work_dir))

    # -- cancellation ------------------------------------------------------
    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    # -- logging -----------------------------------------------------------
    def log(self, level: int, msg: str) -> None:
        msg = mask(msg)
        self.reporter.log(level, msg)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.path("run.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {logging.getLevelName(level):<7} {msg}\n")
        except OSError:
            pass

    def debug(self, msg: str) -> None:
        self.log(logging.DEBUG, msg)

    def info(self, msg: str) -> None:
        self.log(logging.INFO, msg)

    def warn(self, msg: str) -> None:
        self.log(logging.WARNING, msg)

    def error(self, msg: str) -> None:
        self.log(logging.ERROR, msg)
