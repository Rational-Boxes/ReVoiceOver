"""DeepInfra API token lookup: environment, then a ``.env`` file, then the keyring.

``.env`` is searched in the current directory and its parents, then in
``~/.config/narr/.env`` — unless ``use_env_file`` pinned a specific file
(the ``--env-file`` option). Both ``DEEPINFRA_TOKEN`` and ``DEEPINFRA_KEY`` are
accepted, in that order of preference.

The token is never written to work dirs, config files or logs. Anything that
might echo text from an HTTP exchange should pass it through ``mask``.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Literal

ENV_VAR = "DEEPINFRA_TOKEN"
ENV_VARS = (ENV_VAR, "DEEPINFRA_KEY")
DOTENV = ".env"

_env_file: Path | None = None
KEYRING_SERVICE = "narr"
KEYRING_USER = "deepinfra"

TokenSource = Literal["env", "dotenv", "keyring", "none"]


class MissingTokenError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            f"No DeepInfra token. Set {ENV_VAR} (in the environment or a .env file), "
            "run `narr token set`, "
            "or use Settings → DeepInfra token… in the GUI."
        )


@functools.cache
def keyring_token() -> str | None:
    """The stored token; cached because ``mask`` runs on every log line."""
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or None
    except Exception:  # no backend, locked keyring, D-Bus unavailable…
        return None


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` parser: comments, blank lines, ``export``, quotes."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key.strip()] = value
    return out


def use_env_file(path: Path | None) -> None:
    """Read the token from exactly this file instead of searching for ``.env``."""
    global _env_file
    if path is not None and not path.is_file():
        raise FileNotFoundError(f"env file {path} does not exist")
    _env_file = path.resolve() if path is not None else None
    dotenv_token.cache_clear()


def dotenv_candidates() -> list[Path]:
    from narration.config import user_config_dir

    if _env_file is not None:
        return [_env_file]

    cwd = Path.cwd()
    return [d / DOTENV for d in (cwd, *cwd.parents)] + [user_config_dir() / DOTENV]


@functools.cache
def dotenv_token() -> tuple[str, Path] | None:
    """Token from the first ``.env`` that defines one; cached like the keyring."""
    for path in dotenv_candidates():
        try:
            values = parse_dotenv(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        for name in ENV_VARS:
            if values.get(name, "").strip():
                return values[name].strip(), path
    return None


def get_token() -> tuple[str | None, TokenSource]:
    for name in ENV_VARS:
        env = os.environ.get(name, "").strip()
        if env:
            return env, "env"
    if (found := dotenv_token()) is not None:
        return found[0], "dotenv"
    stored = keyring_token()
    if stored:
        return stored, "keyring"
    return None, "none"


def require_token() -> str:
    token, _ = get_token()
    if not token:
        raise MissingTokenError()
    return token


def set_keyring_token(token: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token.strip())
    keyring_token.cache_clear()


def delete_keyring_token() -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except PasswordDeleteError:
        pass
    keyring_token.cache_clear()


def mask(text: str) -> str:
    """Replace any occurrence of the active token in ``text``."""
    token, _ = get_token()
    if token and token in text:
        text = text.replace(token, "****")
    return text
