"""`narr` command line."""

from __future__ import annotations

import asyncio
import getpass
from pathlib import Path
from typing import Annotated

import typer

from narration import credentials, stages
from narration.config import load_config
from narration.context import RunContext, default_work_dir
from narration.pipeline import Outcome, run_pipeline
from narration.progress import RichReporter

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Rebuild a video's narration in a cloned voice.")
token_app = typer.Typer(no_args_is_help=True, help="Manage the DeepInfra API token.")
app.add_typer(token_app, name="token")


@app.callback()
def _global_options(
    env_file: Annotated[Path | None, typer.Option(
        "--env-file", exists=True, dir_okay=False,
        help="Read DEEPINFRA_TOKEN / DEEPINFRA_KEY from this file instead of searching "
             "for .env")] = None,
) -> None:
    credentials.use_env_file(env_file)

VideoArg = Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Source video")]
WorkOpt = Annotated[Path | None, typer.Option("--work-dir", "-w",
                                              help="Default: ./work/<video-stem>")]


def _check_stage(name: str | None) -> str | None:
    if name is not None and name not in stages.STAGE_NAMES:
        raise typer.BadParameter(f"expected one of: {', '.join(stages.STAGE_NAMES)}")
    return name


def _work_dir(video: Path, work_dir: Path | None) -> Path:
    return work_dir or default_work_dir(video, Path.cwd())


@app.command()
def run(
    video: VideoArg,
    work_dir: WorkOpt = None,
    from_: Annotated[str | None, typer.Option("--from", callback=_check_stage,
                                              help="First stage to run")] = None,
    to: Annotated[str | None, typer.Option("--to", callback=_check_stage,
                                           help="Last stage to run")] = None,
    no_review: Annotated[bool, typer.Option("--no-review",
                                            help="Don't pause after the clean stage")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the pipeline (or a range of stages) on VIDEO."""
    work = _work_dir(video, work_dir)
    ctx = RunContext(video=video, work_dir=work, config=load_config(video, work),
                     reporter=RichReporter(verbose))
    try:
        result = asyncio.run(run_pipeline(ctx, start=from_, stop=to, review=not no_review))
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    if result.outcome is Outcome.REVIEW:
        typer.echo(f"Review {work / 'script.md'}, edit {work / 'script.json'}, then:\n"
                   f"  narr run {video} --from voice")
    raise typer.Exit(0 if result.outcome in (Outcome.COMPLETED, Outcome.REVIEW) else 1)


@app.command()
def status(video: VideoArg, work_dir: WorkOpt = None) -> None:
    """Show each stage's status for VIDEO."""
    work = _work_dir(video, work_dir)
    for name, st in stages.project_status(video.resolve(), work.resolve()).items():
        line = f"{name:<11} {st.value}"
        if st is stages.Status.FAILED and (err := stages.stage_error(work, name)):
            line += f"  ({err})"
        typer.echo(line)


@app.command()
def gui(video: Annotated[Path | None, typer.Argument(dir_okay=False)] = None) -> None:
    """Open the desktop app, optionally creating a project for VIDEO."""
    from narration.gui.app import main

    main([str(video)] if video else [])


@token_app.command("set")
def token_set() -> None:
    """Store the token in the system keyring."""
    token = getpass.getpass("DeepInfra token: ").strip()
    if not token:
        raise typer.BadParameter("empty token")
    credentials.set_keyring_token(token)
    typer.echo("Stored in the system keyring.")


@token_app.command("clear")
def token_clear() -> None:
    """Remove the token from the system keyring."""
    credentials.delete_keyring_token()
    typer.echo("Removed from the system keyring.")


@token_app.command("status")
def token_status() -> None:
    """Show where the token is coming from (never prints it)."""
    _, source = credentials.get_token()
    if source == "dotenv" and (found := credentials.dotenv_token()):
        typer.echo(f"from {found[1]}")
        return
    typer.echo({"env": "from the environment", "keyring": "from the system keyring",
                "none": "not set"}.get(source, source))


if __name__ == "__main__":
    app()
