"""DeepInfra API token lookup: environment first, then the system keyring.

The token is never written to work dirs, config files or logs. Anything that
might echo text from an HTTP exchange should pass it through ``mask``.
"""

from __future__ import annotations

import functools
import os
from typing import Literal

ENV_VAR = "DEEPINFRA_TOKEN"
KEYRING_SERVICE = "narr"
KEYRING_USER = "deepinfra"

TokenSource = Literal["env", "keyring", "none"]


class MissingTokenError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            f"No DeepInfra token. Set {ENV_VAR}, run `narr token set`, "
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


def get_token() -> tuple[str | None, TokenSource]:
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env, "env"
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
