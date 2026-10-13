from pathlib import Path

from narration import artifacts, stages
from narration.stages import Status


def _complete(stage_name: str, video: Path, work: Path) -> None:
    stage = stages.get(stage_name)
    for out in stage.outputs(video, work):
        out.write_text("x")
    artifacts.write_manifest(work, stage_name, ok=True,
                             inputs_digest=artifacts.digest_files(stage.inputs(video, work)))


def test_status_transitions(tmp_path: Path):
    video = tmp_path / "v.mp4"
    video.write_text("video")
    work = tmp_path / "work"
    work.mkdir()

    st = stages.project_status(video, work)
    assert set(st.values()) == {Status.MISSING}

    _complete("extract", video, work)
    _complete("transcribe", video, work)
    st = stages.project_status(video, work)
    assert st["extract"] is Status.DONE and st["transcribe"] is Status.DONE
    assert st["segment"] is Status.MISSING

    # Re-running extract rewrites chunks.json -> transcribe is now stale.
    chunks = work / "chunks.json"
    chunks.write_text("changed!")
    st = stages.project_status(video, work)
    assert st["extract"] is Status.DONE
    assert st["transcribe"] is Status.STALE


def test_upstream_stale_propagates(tmp_path: Path):
    video = tmp_path / "v.mp4"
    video.write_text("video")
    work = tmp_path / "work"
    work.mkdir()
    _complete("extract", video, work)
    _complete("transcribe", video, work)
    video.write_text("a different video")  # extract's input changed
    st = stages.project_status(video, work)
    assert st["extract"] is Status.STALE
    assert st["transcribe"] is Status.STALE


def test_failed(tmp_path: Path):
    artifacts.write_manifest(tmp_path, "extract", ok=False, inputs_digest="", error="E")
    assert stages.project_status(tmp_path / "v", tmp_path)["extract"] is Status.FAILED
    assert stages.stage_error(tmp_path, "extract") == "E"
