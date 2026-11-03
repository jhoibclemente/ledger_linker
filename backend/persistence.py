"""PostgreSQL for free hosting; SQLite remains an explicit local-dev option."""
import os
import sqlite3
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
DATABASE_ERRORS = (sqlite3.Error, psycopg.Error)
TABLES = ("users", "sessions", "submissions", "files", "settings", "rate_limits", "storage_cleanup")


def remote():
    mode = os.environ.get("STORAGE_BACKEND", "sqlite")
    values = [os.environ.get(name) for name in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")]
    if mode not in {"sqlite", "supabase"}:
        raise RuntimeError("STORAGE_BACKEND must be sqlite or supabase.")
    if mode == "supabase" and not all(values):
        raise RuntimeError("Supabase mode requires DATABASE_URL, SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
    if mode == "sqlite" and any(values):
        raise RuntimeError("Remote credentials require STORAGE_BACKEND=supabase. Refusing a local-storage fallback.")
    if mode == "supabase":
        # A bad URL must never be interpolated into an error (it contains a password).
        try:
            parts = urlsplit(values[0])
            if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
                raise ValueError
            options = conninfo_to_dict(values[0])
            sslmode = options.get("sslmode", "require")
            loopback = options.get("host") in {"localhost", "127.0.0.1", "::1"} and options.get("hostaddr") in {None, "127.0.0.1", "::1"}
            if not loopback and sslmode not in {"require", "verify-ca", "verify-full"}:
                raise ValueError
        except Exception:
            raise RuntimeError("DATABASE_URL must be a PostgreSQL URL with TLS enabled.") from None
    return mode == "supabase"


class Connection:
    def __init__(self, connection, postgres):
        self.connection = connection
        self.postgres = postgres

    def execute(self, query, parameters=()):
        # All queries are developer-owned SQL; values remain bound parameters.
        if self.postgres:
            query = query.replace("?", "%s")
        return self.connection.execute(query, parameters)

    def executescript(self, script):
        # Schema declarations contain no string literals with semicolons.
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)


@contextmanager
def database(path):
    postgres = remote()
    if postgres:
        options = conninfo_to_dict(os.environ["DATABASE_URL"])
        options.setdefault("sslmode", "require")
        options.setdefault("connect_timeout", "10")
        connection = psycopg.connect(**options, row_factory=dict_row,
                                     prepare_threshold=None)
    else:
        connection = sqlite3.connect(path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            if postgres:
                connection.execute("SET search_path TO ledger_private, pg_catalog")
            yield Connection(connection, postgres)
    finally:
        connection.close()


def create_schema(db):
    if db.postgres:
        db.execute("CREATE SCHEMA IF NOT EXISTS ledger_private")
        db.execute("REVOKE ALL ON SCHEMA ledger_private FROM PUBLIC")
        binary = "BYTEA"
    else:
        db.execute("PRAGMA journal_mode=WAL")
        binary = "BLOB"
    db.executescript(f"""
      CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','admin')),
        created_at BIGINT NOT NULL);
      CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf TEXT NOT NULL, expires BIGINT NOT NULL);
      CREATE TABLE IF NOT EXISTS submissions (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), created_at BIGINT NOT NULL,
        title TEXT NOT NULL, status TEXT NOT NULL, records TEXT NOT NULL, file_count INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
        filename TEXT NOT NULL, content {binary} NOT NULL, object_key TEXT);
      CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires BIGINT NOT NULL);
      CREATE TABLE IF NOT EXISTS storage_cleanup (object_key TEXT PRIMARY KEY, created_at BIGINT NOT NULL);
      CREATE INDEX IF NOT EXISTS submissions_owner ON submissions(user_id, created_at);
    """)
    if not db.postgres:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(files)").fetchall()}
        if "object_key" not in columns:
            db.execute("ALTER TABLE files ADD COLUMN object_key TEXT")
    else:
        for table in TABLES:
            db.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
