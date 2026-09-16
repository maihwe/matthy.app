"""``config.env`` — the loud-failure contract of 12 §4.

The rule these enforce: **a missing required variable fails the boot, at the
point of reading it, naming itself.** The alternative — a default that looks
plausible — produces a process that starts successfully and is wrong, which is
strictly worse than one that refuses to start.

A present-but-unparseable value is treated the same as a missing one. A default
covers *absence*; it must not silently absorb an explicit bad value, because
``EMAIL_PORT=abc`` is a typo someone needs to see, not a request for port 1025.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.env import env, env_bool, env_int, env_list, require_env

pytestmark = pytest.mark.unit


class TestRequireEnv:
    def test_returns_the_value_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_REQUIRED_THING", "a-value")
        assert require_env("SOME_REQUIRED_THING") == "a-value"

    def test_names_the_missing_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The message must name the variable.

        A boot failure that says only "missing configuration" sends the reader
        to the whole environment; one that says which variable named it ends the
        investigation immediately.
        """
        monkeypatch.delenv("SOME_REQUIRED_THING", raising=False)
        with pytest.raises(ImproperlyConfigured, match="SOME_REQUIRED_THING"):
            require_env("SOME_REQUIRED_THING")

    def test_empty_string_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A variable exported as ``FOO=`` is nearly always a shell mistake.

        Silently honouring it is how a blank SECRET_KEY reaches production —
        Django accepts an empty SECRET_KEY and then signs every session with
        nothing.
        """
        monkeypatch.setenv("SOME_REQUIRED_THING", "")
        with pytest.raises(ImproperlyConfigured, match="SOME_REQUIRED_THING"):
            require_env("SOME_REQUIRED_THING")

    def test_whitespace_only_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A trailing space in a compose file or a CI secret is invisible."""
        monkeypatch.setenv("SOME_REQUIRED_THING", "   ")
        with pytest.raises(ImproperlyConfigured):
            require_env("SOME_REQUIRED_THING")


class TestEnv:
    def test_absent_with_a_default_returns_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SOME_OPTIONAL_THING", raising=False)
        assert env("SOME_OPTIONAL_THING", default="fallback") == "fallback"

    def test_absent_without_a_default_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``env`` is the optional reader; ``None`` is a real answer, not an
        error. Only ``require_env`` fails."""
        monkeypatch.delenv("SOME_OPTIONAL_THING", raising=False)
        assert env("SOME_OPTIONAL_THING") is None

    def test_empty_string_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Consistent with ``require_env``: empty means unset, everywhere."""
        monkeypatch.setenv("SOME_OPTIONAL_THING", "")
        assert env("SOME_OPTIONAL_THING", default="fallback") == "fallback"


class TestEnvBool:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "True", "yes", "on", " yes "])
    def test_accepts_the_usual_truthy_spellings(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("SOME_FLAG", raw)
        assert env_bool("SOME_FLAG") is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", ""])
    def test_accepts_the_usual_falsy_spellings(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("SOME_FLAG", raw)
        assert env_bool("SOME_FLAG") is False

    def test_rejects_an_unrecognised_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``DEBUG=maybe`` must not be guessed at.

        Python's ``bool("maybe")`` is ``True``. A reader that fell back to
        truthiness would enable DEBUG in production on the strength of a typo.
        """
        monkeypatch.setenv("SOME_FLAG", "maybe")
        with pytest.raises(ImproperlyConfigured, match="SOME_FLAG"):
            env_bool("SOME_FLAG")

    def test_absent_uses_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOME_FLAG", raising=False)
        assert env_bool("SOME_FLAG", default=False) is False
        assert env_bool("SOME_FLAG", default=True) is True

    def test_absent_without_a_default_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No default for a flag whose safe value the reader cannot know."""
        monkeypatch.delenv("SOME_FLAG", raising=False)
        with pytest.raises(ImproperlyConfigured, match="SOME_FLAG"):
            env_bool("SOME_FLAG")


class TestEnvInt:
    def test_parses_a_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_COUNT", "42")
        assert env_int("SOME_COUNT") == 42

    def test_rejects_a_non_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_COUNT", "many")
        with pytest.raises(ImproperlyConfigured, match="SOME_COUNT"):
            env_int("SOME_COUNT")

    def test_a_default_does_not_absorb_a_bad_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The distinction the whole module rests on.

        ``EMAIL_PORT=abc`` is a typo to be reported, not a request for the
        default port. A default that swallowed it would turn a visible mistake
        into a silent one.
        """
        monkeypatch.setenv("SOME_COUNT", "many")
        with pytest.raises(ImproperlyConfigured, match="SOME_COUNT"):
            env_int("SOME_COUNT", default=1)

    def test_rejects_a_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``int("1.5")`` raises, and it must — 02 §5.1 forbids fractional
        minor units, so a float reaching a money setting is a defect to stop
        here rather than to round somewhere later."""
        monkeypatch.setenv("SOME_COUNT", "1.5")
        with pytest.raises(ImproperlyConfigured, match="SOME_COUNT"):
            env_int("SOME_COUNT")

    def test_absent_uses_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOME_COUNT", raising=False)
        assert env_int("SOME_COUNT", default=7) == 7


class TestEnvList:
    def test_splits_on_commas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOME_LIST", "a,b,c")
        assert env_list("SOME_LIST") == ["a", "b", "c"]

    def test_strips_whitespace_and_discards_blanks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A trailing comma is the most common way to write a list.

        Without discarding blanks it produces an empty ALLOWED_HOSTS entry,
        which Django treats as a host that matches nothing — a confusing
        failure at request time rather than at boot.
        """
        monkeypatch.setenv("SOME_LIST", "a, b ,,c,")
        assert env_list("SOME_LIST") == ["a", "b", "c"]

    def test_empty_becomes_an_empty_list_not_a_list_containing_blank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOME_LIST", "")
        assert env_list("SOME_LIST") == []

    def test_absent_uses_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOME_LIST", raising=False)
        assert env_list("SOME_LIST", default=["x"]) == ["x"]
