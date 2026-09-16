"""Environment-variable reading, with a loud failure mode.

12 §4 and 10 §7: *"Configuration is read from environment at process start; a
missing required variable fails the boot loudly rather than defaulting to
something insecure."* That is the whole job of this module.

Deliberately not a settings library. A dependency here would hide the
difference between "this variable was set to the empty string" and "this
variable was never set", which is precisely the distinction that matters when
a production secret is missing at 3am. `require_env` refuses both.
"""

from __future__ import annotations

import os
from typing import Final

from django.core.exceptions import ImproperlyConfigured

__all__ = ["env", "env_bool", "env_int", "env_list", "require_env"]

_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})


def env(name: str, default: str | None = None) -> str | None:
    """Read an optional variable.

    An empty string is treated as unset. A variable exported as ``FOO=`` is
    almost always a shell or compose mistake rather than a deliberate empty
    value, and silently honouring it is how a blank ``SECRET_KEY`` reaches
    production.
    """
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value


def require_env(name: str) -> str:
    """Read a required variable, or fail the boot.

    Raises:
        ImproperlyConfigured: if the variable is unset or empty.
    """
    value = env(name)
    if value is None:
        raise ImproperlyConfigured(
            f"Required environment variable {name!r} is not set. "
            f"See .env.example for the full list of variables this project expects."
        )
    return value


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean variable.

    Raises:
        ImproperlyConfigured: if the value is set but unrecognised. Guessing at
            ``DEBUG=maybe`` is how a debug page ends up in production (12 §4).
    """
    raw = env(name)
    if raw is None:
        return default
    normalised = raw.strip().lower()
    if normalised in _TRUE:
        return True
    if normalised in _FALSE:
        return False
    raise ImproperlyConfigured(
        f"Environment variable {name!r} must be a boolean, got {raw!r}. "
        f"Accepted: {'/'.join(sorted(_TRUE | _FALSE))}."
    )


def env_int(name: str, default: int) -> int:
    """Read an integer variable.

    Raises:
        ImproperlyConfigured: if the value is set but not an integer.
    """
    raw = env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"Environment variable {name!r} must be an integer, got {raw!r}."
        ) from exc


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    """Read a comma-separated list variable, discarding blank entries."""
    raw = env(name)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]
