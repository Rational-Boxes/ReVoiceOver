import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from narration.config import CONFIG_NAME, load_config, save_overrides, user_config_dir


def test_defaults():
    cfg = load_config()
    assert cfg.fit.timing == "anchor"
    assert cfg.segment.pause_split_s == 0.6


def test_layers_override_in_order(tmp_path: Path):
    user = user_config_dir()
    user.mkdir(parents=True)
    (user / "config.toml").write_text('[fit]\ntiming = "compact"\nmax_atempo = 1.2\n')
    video = tmp_path / "v.mp4"
    video.touch()
    (tmp_path / CONFIG_NAME).write_text("[fit]\nmax_atempo = 1.1\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / CONFIG_NAME).write_text('[fit]\ntiming = "anchor"\n')

    cfg = load_config(video, work)
    assert cfg.fit.timing == "anchor"      # per-video wins
    assert cfg.fit.max_atempo == 1.1       # project beats user


def test_unknown_key_rejected(tmp_path: Path):
    work = tmp_path
    (work / CONFIG_NAME).write_text("[fit]\ntimng = 'anchor'\n")
    with pytest.raises(ValidationError):
        load_config(None, work)


def test_save_overrides_writes_only_differences(tmp_path: Path):
    base = load_config()
    cfg = load_config()
    cfg.fit.timing = "compact"
    out = tmp_path / CONFIG_NAME
    save_overrides(out, cfg, base)
    assert tomllib.loads(out.read_text()) == {"fit": {"timing": "compact"}}


def test_old_compact_gap_becomes_pad_after(tmp_path: Path):
    (tmp_path / CONFIG_NAME).write_text("[fit]\ncompact_gap_s = 0.5\n")
    assert load_config(None, tmp_path).fit.compact_pad_after_s == 0.5
