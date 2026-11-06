"""Verify bootstrap permissions against a disposable real PostgreSQL database."""

from __future__ import annotations

import secrets
from uuid import uuid4

import psycopg
import pytest
from django.conf import settings
from psycopg import sql

from scripts.aws_initialize_database import initialize

pytestmark = pytest.mark.integration


def test_staging_role_can_migrate_but_cannot_administer_postgres() -> None:
    suffix = uuid4().hex[:12]
    database = f"aws_bootstrap_test_{suffix}"
    username = f"aws_bootstrap_user_{suffix}"
    password = secrets.token_urlsafe(32)
    configured = settings.DATABASES["default"]
    options = {
        "host": configured["HOST"],
        "port": configured["PORT"],
        "user": configured["USER"],
        "password": configured["PASSWORD"],
    }
    with psycopg.connect(**options, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            with psycopg.connect(**options, dbname=database) as connection:
                initialize(connection, username, password, database)
            with psycopg.connect(
                host=configured["HOST"],
                port=configured["PORT"],
                dbname=database,
                user=username,
                password=password,
                autocommit=True,
            ) as runtime:
                row = runtime.execute(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
                assert row == (False, False, False, False)
                runtime.execute("CREATE TABLE bootstrap_test (id bigint PRIMARY KEY)")
                runtime.execute("INSERT INTO bootstrap_test VALUES (1)")
                assert runtime.execute("SELECT id FROM bootstrap_test").fetchone() == (1,)
                assert runtime.execute(
                    "SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'"
                ).fetchone() == ("pg_trgm",)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    runtime.execute("CREATE ROLE bootstrap_forbidden_role")
            with psycopg.connect(**options, dbname=database) as repeated:
                with pytest.raises(ValueError, match="already exists"):
                    initialize(repeated, username, "must-not-replace-the-password", database)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(username)))
