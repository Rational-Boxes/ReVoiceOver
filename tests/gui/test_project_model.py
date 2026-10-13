import tomllib

import pytest

from narration.config import CONFIG_NAME
from narration.gui.models.project_model import Project, ProjectError, is_project_dir


def test_create_open_and_overrides(tmp_path):
    video = tmp_path / "talk.mp4"
    video.write_text("v")
    project = Project.create(video, overrides={"fit": {"timing": "compact"}})
    assert project.work_dir == tmp_path / "work" / "talk"
    assert is_project_dir(project.work_dir)
    assert tomllib.loads((project.work_dir / CONFIG_NAME).read_text()) == {
        "fit": {"timing": "compact"}}

    reopened = Project.open(project.work_dir)
    assert reopened.video == video.resolve()
    assert reopened.config().fit.timing == "compact"


def test_work_dir_owned_by_other_video(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_text("a")
    b.write_text("b")
    work = tmp_path / "shared"
    Project.create(a, work)
    with pytest.raises(ProjectError):
        Project.create(b, work)


def test_open_missing_video(tmp_path):
    video = tmp_path / "gone.mp4"
    video.write_text("v")
    project = Project.create(video)
    video.unlink()
    with pytest.raises(ProjectError):
        Project.open(project.work_dir)


def test_cli_work_dir_opens_in_gui(tmp_path):
    from narration.config import load_config
    from narration.context import RunContext

    video = tmp_path / "talk.mp4"
    video.write_text("v")
    ctx = RunContext(video=video, work_dir=tmp_path / "cli-work", config=load_config())
    assert Project.open(ctx.work_dir).video == video.resolve()
