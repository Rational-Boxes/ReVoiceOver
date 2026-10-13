"""Progress reporting shared by the CLI (rich) and the GUI (wx events)."""

from __future__ import annotations

import logging
from typing import Protocol


class ProgressReporter(Protocol):
    def stage_started(self, stage: str, total: int | None) -> None: ...
    def set_total(self, stage: str, total: int | None) -> None: ...
    def advance(self, stage: str, n: int = 1, detail: str = "") -> None: ...
    def unit_updated(self, unit_id: str, status: str) -> None: ...
    def log(self, level: int, msg: str) -> None: ...
    def stage_finished(self, stage: str, ok: bool) -> None: ...


class NullReporter:
    def stage_started(self, stage: str, total: int | None) -> None:
        pass

    def set_total(self, stage: str, total: int | None) -> None:
        pass

    def advance(self, stage: str, n: int = 1, detail: str = "") -> None:
        pass

    def unit_updated(self, unit_id: str, status: str) -> None:
        pass

    def log(self, level: int, msg: str) -> None:
        pass

    def stage_finished(self, stage: str, ok: bool) -> None:
        pass


class RichReporter:
    """Console reporter for the CLI."""

    def __init__(self, verbose: bool = False) -> None:
        from rich.console import Console
        from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn

        self.console = Console(stderr=True)
        self.min_level = logging.DEBUG if verbose else logging.INFO
        self._progress = Progress(
            TextColumn("[bold]{task.fields[stage]:<10}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[detail]}"),
            console=self.console,
            transient=True,
        )
        self._tasks: dict[str, TaskID] = {}

    def stage_started(self, stage: str, total: int | None) -> None:
        if not self._tasks:
            self._progress.start()
        self._tasks[stage] = self._progress.add_task("", total=total, stage=stage, detail="")

    def set_total(self, stage: str, total: int | None) -> None:
        task = self._tasks.get(stage)
        if task is not None:
            self._progress.update(task, total=total, completed=0)

    def advance(self, stage: str, n: int = 1, detail: str = "") -> None:
        task = self._tasks.get(stage)
        if task is not None:
            self._progress.update(task, advance=n, detail=detail)

    def unit_updated(self, unit_id: str, status: str) -> None:
        pass

    def log(self, level: int, msg: str) -> None:
        if level < self.min_level:
            return
        style = {logging.WARNING: "yellow", logging.ERROR: "bold red"}.get(level, "")
        self._progress.console.print(msg, style=style, markup=False, highlight=False)

    def stage_finished(self, stage: str, ok: bool) -> None:
        task = self._tasks.pop(stage, None)
        if task is not None:
            self._progress.remove_task(task)
        mark = "[green]✔[/]" if ok else "[red]✘[/]"
        self.console.print(f"{mark} {stage}")
        if not self._tasks:
            self._progress.stop()
