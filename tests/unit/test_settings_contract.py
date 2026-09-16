"""Settings that are security controls, asserted as values.

13 §9's coverage floor does not measure a setting being *wrong*. A misconfigured
hasher, a default-allow permission class or a debug flag left on all leave every
functional test passing. These are the assertions that fail instead.

Importing a settings module directly (rather than through
``django.conf.settings``) is deliberate: it tests what each environment module
computes, not whichever one is active in this run.

**Settings modules are cached in ``sys.modules``**, so a module-level value is
read from the environment exactly once per process. A test that changed an
environment variable and re-imported would get the cached module back and pass
for the wrong reason — ``_fresh_settings`` evicts the ``config.settings``
subtree so the import genuinely re-executes.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType

import pytest
from django.core.exceptions import ImproperlyConfigured

from config import branding
from config.env import env_bool

pytestmark = pytest.mark.unit

_PREFIX = "config.settings"


@contextmanager
def _fresh_settings() -> Iterator[None]:
    """Evict and restore the ``config.settings`` package subtree.

    ``config.settings.base`` is already imported by the time any test runs, so
    ``importlib.import_module`` would return the cached object and never
    re-read the environment. Django's active settings object is unaffected —
    it holds its own reference, not the module entry.
    """
    saved = {name: mod for name, mod in list(sys.modules.items()) if name.startswith(_PREFIX)}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [n for n in sys.modules if n.startswith(_PREFIX)]:
            del sys.modules[name]
        sys.modules.update(saved)


def _settings_module(dotted_path: str) -> ModuleType:
    return importlib.import_module(dotted_path)


class TestPasswordHashing:
    """SEC-AUTH-001 — Argon2id is the first hasher."""

    def test_base_settings_put_argon2_first(self) -> None:
        """Ordering is load-bearing: Django hashes with the first entry.

        A PBKDF2 entry above Argon2 would still verify old hashes correctly and
        would still let every test pass, while every new password was stored
        weaker than the specification requires.
        """
        base = _settings_module("config.settings.base")
        assert base.PASSWORD_HASHERS[0] == "django.contrib.auth.hashers.Argon2PasswordHasher"

    def test_production_keeps_argon2_first(self) -> None:
        production = _settings_module("config.settings.production")
        assert production.PASSWORD_HASHERS[0] == "django.contrib.auth.hashers.Argon2PasswordHasher"

    def test_test_settings_use_a_fast_hasher_deliberately(self) -> None:
        """The suite weakens hashing for speed, and that must stay visible.

        This is the one place the project knowingly runs a weaker control than
        production. Asserting it means the difference is a decision with a
        test behind it, rather than an accident somebody discovers later.
        """
        test_settings = _settings_module("config.settings.test")
        assert test_settings.PASSWORD_HASHERS == ["django.contrib.auth.hashers.MD5PasswordHasher"]


class TestDebugFlag:
    """12 §4 — a debug page in production exposes settings and source."""

    def test_debug_is_off_unless_explicitly_requested(self) -> None:
        """The default is the control. An unset variable must mean "off".

        Asserting ``base.DEBUG`` here would instead assert how this machine's
        environment happens to be configured, so the reader's default is what
        is tested.
        """
        assert env_bool("DJANGO_DEBUG", default=False) is False

    def test_production_refuses_to_boot_with_debug_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DJANGO_DEBUG", "true")
        with _fresh_settings(), pytest.raises(ImproperlyConfigured, match="DJANGO_DEBUG"):
            _settings_module("config.settings.production")

    def test_production_refuses_an_empty_allowed_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no wildcard default.

        An empty list that quietly falls back to localhost is a Host-header
        surface, and the fallback is exactly what makes it survive to
        production without anyone noticing.
        """
        monkeypatch.setenv("DJANGO_DEBUG", "false")
        monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "")
        with _fresh_settings(), pytest.raises(ImproperlyConfigured, match="DJANGO_ALLOWED_HOSTS"):
            _settings_module("config.settings.production")

    def test_staging_cannot_drift_from_production(self) -> None:
        """Staging inherits production rather than restating it.

        A staging module that duplicated the security settings would drift the
        first time one of them changed, and it would drift silently in the
        environment nobody deploys to production from.
        """
        staging = _settings_module("config.settings.staging")
        production = _settings_module("config.settings.production")
        assert staging.SECURE_SSL_REDIRECT == production.SECURE_SSL_REDIRECT
        assert staging.SESSION_COOKIE_SECURE == production.SESSION_COOKIE_SECURE
        assert staging.X_FRAME_OPTIONS == production.X_FRAME_OPTIONS


class TestAPIHardening:
    """SEC-AUTHZ-001 and 04 §1 — the defaults a view inherits."""

    def test_default_permission_is_deny(self) -> None:
        """Default-deny.

        S6 asserts every view declares its own permission class. Until that
        ratchet covers a given module, this is what keeps a view with no
        declaration from being an *open* endpoint.
        """
        base = _settings_module("config.settings.base")
        assert base.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] == [
            "rest_framework.permissions.IsAuthenticated"
        ]

    def test_errors_use_the_single_envelope_handler(self) -> None:
        base = _settings_module("config.settings.base")
        assert (
            base.REST_FRAMEWORK["EXCEPTION_HANDLER"]
            == "config.api.exceptions.envelope_exception_handler"
        )

    def test_pagination_is_cursor_based(self) -> None:
        """04 §1.4 — offset pagination is not offered at all.

        Keyset pagination is not a preference here: a ledger that shifts under
        an offset page repeats and skips rows, and the client cannot detect it.
        """
        base = _settings_module("config.settings.base")
        assert (
            base.REST_FRAMEWORK["DEFAULT_PAGINATION_CLASS"] == "config.api.pagination.CursorPagination"
        )

    def test_money_is_never_parsed_by_a_browser_form_parser(self) -> None:
        """Only JSON is accepted.

        04 §1.3 puts money on the wire as a decimal *string*, because a JSON
        number is an IEEE-754 double the moment any client touches it.
        """
        base = _settings_module("config.settings.base")
        assert base.REST_FRAMEWORK["DEFAULT_PARSER_CLASSES"] == ["rest_framework.parsers.JSONParser"]

    def test_a_session_cannot_be_authenticated_anonymously(self) -> None:
        """``UNAUTHENTICATED_USER: None`` is *not* set.

        Setting it to ``None`` makes ``request.user`` a ``None`` that passes
        ``is not None`` checks while failing every attribute access, which
        turns a permissions bug into an ``AttributeError`` 500.
        """
        base = _settings_module("config.settings.base")
        assert "UNAUTHENTICATED_USER" not in base.REST_FRAMEWORK


class TestSessionHardening:
    """SEC-SES-005 — a non-default cookie name."""

    def test_session_cookie_name_is_not_the_django_default(self) -> None:
        """A cookie collision with another Django app on the same domain can
        cause session confusion, and the default name is what collides."""
        base = _settings_module("config.settings.base")
        assert base.SESSION_COOKIE_NAME != "sessionid"
        assert base.SESSION_COOKIE_HTTPONLY is True
        assert base.SESSION_COOKIE_SAMESITE == "Lax"

    def test_atomic_requests_is_not_enabled(self) -> None:
        """02 §5.3 — transaction boundaries are explicit, never implicit.

        ``ATOMIC_REQUESTS`` wraps a request in a transaction that ends at the
        response, which silently commits work the service layer was supposed to
        decide about.
        """
        base = _settings_module("config.settings.base")
        assert base.DATABASES["default"].get("ATOMIC_REQUESTS", False) is False


class TestPasswordPolicy:
    """SEC-AUTH-004 — enforced at registration, and enforced locally."""

    def test_the_breach_corpus_is_local(self) -> None:
        """The requirement's explicit condition: "so no plaintext is sent to a
        third-party service".

        Django's ``CommonPasswordValidator`` carries a corpus of common passwords
        inside the package, so the check costs no network call. The usual
        alternative — a k-anonymity query against a breach API — sends a prefix
        of every password a member chooses to someone else's server, which is the
        thing this requirement forbids. Asserted as an absence so that a later
        swap to a networked validator has to delete this test to proceed.
        """
        base = _settings_module("config.settings.base")
        names = [validator["NAME"] for validator in base.AUTH_PASSWORD_VALIDATORS]

        assert "django.contrib.auth.password_validation.CommonPasswordValidator" in names
        assert not [name for name in names if "pwned" in name.lower()]

    def test_the_similarity_check_knows_what_identifies_a_member(self) -> None:
        """P1-23.

        Django's default attribute list is ``("username", "first_name",
        "last_name", "email")``. This model has no first or last name, its
        ``username`` is nullable and is not an identity (``USERNAME_FIELD`` is
        ``phone``), and its email is optional — so on the default list the
        validator would compare against almost nothing. Asserted as a subset
        rather than an exact set: adding a further attribute is a hardening
        nobody needs to be told about, while *removing* one of these two silently
        returns the validator to checking nothing.
        """
        base = _settings_module("config.settings.base")
        similarity = next(
            validator
            for validator in base.AUTH_PASSWORD_VALIDATORS
            if "Similarity" in validator["NAME"]
        )

        assert {"full_name", "phone"} <= set(similarity["OPTIONS"]["user_attributes"])

    def test_the_minimum_length_is_not_a_default(self) -> None:
        """Django's default is eight; eight characters of a memorable word is a
        password that a corpus check can be relied on to catch only if the word
        is common, and a Nigerian phone number is eleven digits. Twelve is the
        floor this project chose, and it is stated rather than inherited."""
        base = _settings_module("config.settings.base")
        minimum = next(
            validator
            for validator in base.AUTH_PASSWORD_VALIDATORS
            if "MinimumLength" in validator["NAME"]
        )

        assert minimum["OPTIONS"]["min_length"] == 12


class TestLockout:
    """NFR-SEC-002 — the backoff can be reconfigured, not switched off."""

    def test_the_suite_does_not_weaken_the_backoff(self) -> None:
        """As strong in the test environment as in production.

        There is deliberately no enable/disable flag. The rate limiter had one
        and it was removed (P1-16) for reasons that apply here unchanged: a
        switch that turns a security control off is the switch that ends up set
        in production, and it would not even solve the problem it was written for.

        So the only way to change this is to change the numbers — which is what
        ``tests/integration/test_lockout_service.py`` does per test, and what an
        incident would do per deploy.
        """
        test_settings = _settings_module("config.settings.test")
        base = _settings_module("config.settings.base")

        assert test_settings.LOCKOUT == base.LOCKOUT


class TestBranding:
    """P1-1 — the brand is IHWEZA; the package stays matthy.app."""

    def test_brand_and_platform_names_are_distinct(self) -> None:
        assert branding.BRAND_NAME == "IHWEZA"
        assert branding.BRAND_TAGLINE == "Together We Move."
        assert branding.PLATFORM_NAME == "matthy.app"

    def test_brand_colours_are_valid_hex(self) -> None:
        for value in (branding.BRAND_COLOR_PRIMARY, branding.BRAND_COLOR_ACCENT):
            assert value.startswith("#")
            assert len(value) == 7
            int(value[1:], 16)  # raises ValueError on a malformed value
