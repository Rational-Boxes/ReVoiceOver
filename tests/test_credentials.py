from narration import credentials
from narration.credentials import parse_dotenv

REAL_DOTENV_TOKEN = credentials.dotenv_token  # captured before conftest stubs it


def test_parse_dotenv():
    text = """
# comment
export DEEPINFRA_TOKEN="quoted value"
DEEPINFRA_KEY=plain # trailing comment
EMPTY=
NOEQUALS
"""
    assert parse_dotenv(text) == {
        "DEEPINFRA_TOKEN": "quoted value", "DEEPINFRA_KEY": "plain", "EMPTY": ""}


def test_lookup_order(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "keyring_token", lambda: "from-keyring")
    assert credentials.get_token() == ("from-keyring", "keyring")

    monkeypatch.setattr(credentials, "dotenv_token", lambda: ("from-dotenv", tmp_path / ".env"))
    assert credentials.get_token() == ("from-dotenv", "dotenv")

    monkeypatch.setenv("DEEPINFRA_KEY", "from-env-alias")
    assert credentials.get_token() == ("from-env-alias", "env")
    monkeypatch.setenv("DEEPINFRA_TOKEN", "from-env")
    assert credentials.get_token() == ("from-env", "env")


def test_dotenv_search(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "dotenv_token", REAL_DOTENV_TOKEN)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("DEEPINFRA_KEY=abc\n")
    monkeypatch.chdir(nested)
    credentials.dotenv_token.cache_clear()
    assert credentials.dotenv_token() == ("abc", tmp_path / ".env")
    credentials.dotenv_token.cache_clear()


def test_mask(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_TOKEN", "s3cret")
    assert credentials.mask("Bearer s3cret!") == "Bearer ****!"


def test_use_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "dotenv_token", REAL_DOTENV_TOKEN)
    (tmp_path / ".env").write_text("DEEPINFRA_KEY=searched\n")
    pinned = tmp_path / "other.env"
    pinned.write_text("DEEPINFRA_TOKEN=pinned\n")
    monkeypatch.chdir(tmp_path)
    try:
        credentials.use_env_file(pinned)
        assert credentials.get_token() == ("pinned", "dotenv")
        credentials.use_env_file(None)
        assert credentials.get_token() == ("searched", "dotenv")
    finally:
        credentials.use_env_file(None)


def test_use_env_file_missing(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        credentials.use_env_file(tmp_path / "nope.env")
