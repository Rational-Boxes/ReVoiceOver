"""Run a range of stages in order, writing a manifest after each one."""

from __future__ import annotations

import inspect
import traceback
from dataclasses import dataclass
from enum import StrEnum

from narration import artifacts, stages
from narration.context import Cancelled, RunContext
from narration.stages import REVIEW_AFTER, STAGES, StageNotImplemented


class Outcome(StrEnum):
    COMPLETED = "completed"
    REVIEW = "review"              # stopped at the human review checkpoint
    CANCELLED = "cancelled"
    FAILED = "failed"
    NOT_IMPLEMENTED = "not_implemented"


@dataclass(frozen=True)
class RunResult:
    outcome: Outcome
    last_stage: str | None
    error: str | None = None


async def run_stage(ctx: RunContext, name: str) -> None:
    stage = stages.get(name)
    digest = artifacts.digest_files(stage.inputs(ctx.video, ctx.work_dir))
    ctx.reporter.stage_started(name, None)
    try:
        result = stage.load()(ctx)
        if inspect.isawaitable(result):
            await result
    except Cancelled:
        ctx.reporter.stage_finished(name, False)
        raise
    except StageNotImplemented:
        artifacts.manifest_path(ctx.work_dir, name).unlink(missing_ok=True)
        ctx.reporter.stage_finished(name, False)
        raise
    except Exception as exc:
        artifacts.write_manifest(ctx.work_dir, name, ok=False, inputs_digest=digest,
                                 error=f"{type(exc).__name__}: {exc}")
        ctx.reporter.stage_finished(name, False)
        raise
    # Digest the inputs as they were *before* the stage ran, so an input that
    # changed mid-run leaves the stage stale rather than falsely done.
    artifacts.write_manifest(ctx.work_dir, name, ok=True, inputs_digest=digest)
    ctx.reporter.stage_finished(name, True)


async def run_pipeline(
    ctx: RunContext,
    *,
    start: str | None = None,
    stop: str | None = None,
    review: bool = True,
) -> RunResult:
    """Run stages ``start..stop`` inclusive.

    With ``review`` set, the run pauses after the clean stage unless that is
    where it started from or ``stop`` is ``clean`` itself.
    """
    first = stages.index(start) if start else 0
    last = stages.index(stop) if stop else len(STAGES) - 1
    if first > last:
        raise ValueError(f"start stage {start!r} comes after stop stage {stop!r}")

    name: str | None = None
    for stage in STAGES[first:last + 1]:
        name = stage.name
        try:
            ctx.check_cancelled()
            ctx.info(f"── {stage.title} ──")
            await run_stage(ctx, name)
        except Cancelled:
            ctx.warn(f"cancelled during {name}")
            return RunResult(Outcome.CANCELLED, name)
        except StageNotImplemented as exc:
            ctx.warn(str(exc))
            return RunResult(Outcome.NOT_IMPLEMENTED, name, str(exc))
        except Exception as exc:
            ctx.error(f"{name} failed: {type(exc).__name__}: {exc}")
            ctx.debug(traceback.format_exc())
            return RunResult(Outcome.FAILED, name, f"{type(exc).__name__}: {exc}")
        if review and name == REVIEW_AFTER and name != STAGES[last].name:
            ctx.info("Paused for script review. Edit script.json, then continue from 'voice'.")
            return RunResult(Outcome.REVIEW, name)
    return RunResult(Outcome.COMPLETED, name)
