"""One-time operator task: create the unprivileged staging migration/runtime role."""

from __future__ import annotations

import os

import psycopg
from psycopg import sql


def initialize(connection: psycopg.Connection, username: str, password: str, database: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
        if cursor.fetchone():
            # Refuse to overwrite an existing role/password on repeat initialization.
            raise ValueError("Staging role already exists; inspect it rather than reset it")
        cursor.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
            ).format(sql.Identifier(username), sql.Literal(password))
        )
        cursor.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        cursor.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(username))
        )
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(username)
            )
        )
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def main() -> None:
    username = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    if username != "matthy_staging" or not password:
        raise ValueError("Expected the configured staging role and a nonempty password")
    with psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["RDS_ADMIN_USERNAME"],
        password=os.environ["RDS_ADMIN_PASSWORD"],
        sslmode="require",
        connect_timeout=10,
    ) as connection:
        initialize(connection, username, password, os.environ["POSTGRES_DB"])
    print("Staging database role initialized; run migrations as matthy_staging next.")


if __name__ == "__main__":
    # Avoid dumping a database exception/query containing credentials into task logs.
    try:
        main()
    except Exception:
        print("Database initialization failed. Inspect role state and operator configuration.")
        raise SystemExit(1) from None
