"""Real PostgreSQL integration; objects use an isolated in-memory provider.

Set TEST_POSTGRES_URL only to a disposable test database: this module creates
and drops its ledger_private schema. A localhost URL normally includes
``?sslmode=disable``. Configuration failure tests always run without PostgreSQL.
"""
import csv
import io
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
import pytest
from fastapi.testclient import TestClient

from backend import main, persistence, storage
from tests.conftest import login, upload


@pytest.fixture(autouse=True)
def isolate_remote_environment(monkeypatch):
    for name in ["DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_BUCKET", "APP_ORIGIN"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")


@dataclass
class RemoteState:
    url: str
    objects: dict
    deleted: list
    verified: list


def require_local_test_database(url):
    """Validate libpq's effective URL options before destructive schema cleanup."""
    try:
        options = conninfo_to_dict(url)
    except (psycopg.ProgrammingError, TypeError):
        pytest.fail("TEST_POSTGRES_URL must name a disposable PostgreSQL database on loopback.")
    if (options.get("host") not in {"127.0.0.1", "localhost", "::1"} or
            options.get("hostaddr", "") not in {"", "127.0.0.1", "::1"} or
            options.get("service")):
        pytest.fail("TEST_POSTGRES_URL must target loopback only; remote hosts, hostaddr overrides and services are refused.")


@pytest.fixture
def remote_state(monkeypatch, tmp_path):
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to run PostgreSQL integration tests.")
    require_local_test_database(url)
    # A local host in the URL must not inherit a remote libpq hostaddr/service
    # from the shell. Explicit URL overrides were validated above.
    for selector in ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"]:
        monkeypatch.delenv(selector, raising=False)
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS ledger_private CASCADE")
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SUPABASE_URL", "https://synthetic-project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-test-only")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-password-12345")
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "must-not-exist.sqlite3")
    monkeypatch.setattr(main, "SECURE_COOKIES", False)
    state = RemoteState(url, {}, [], [])

    def ensure_private_bucket():
        state.verified.append(True)

    def put_object(key, content):
        assert key not in state.objects
        state.objects[key] = content

    def get_object(key):
        if key not in state.objects:
            raise storage.StorageError("Statement storage is unavailable. Please try again later.")
        return state.objects[key]

    def delete_objects(keys):
        state.deleted.extend(keys)
        for key in keys:
            state.objects.pop(key, None)

    monkeypatch.setattr(storage, "ensure_private_bucket", ensure_private_bucket)
    monkeypatch.setattr(storage, "upload", put_object)
    monkeypatch.setattr(storage, "download", get_object)
    monkeypatch.setattr(storage, "delete", delete_objects)
    try:
        yield state
        assert not main.DB_PATH.exists(), "Remote mode must never create an ephemeral SQLite database."
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS ledger_private CASCADE")


@pytest.fixture
def remote_client(remote_state):
    with TestClient(main.app) as client:
        yield client, remote_state


def test_postgres_statement_export_and_private_originals(remote_client):
    client, state = remote_client
    user = login(client)
    assert user["role"] == "user"
    result = upload(client)
    assert result.status_code == 200, result.text
    submission = result.json()
    submission_id = submission["id"]
    assert len(submission["records"]) == 6
    assert len(state.objects) == 3
    assert all(key.startswith(user["id"] + "/" + submission_id + "/") for key in state.objects)
    with main.database() as db:
        rows = db.execute("SELECT content,object_key FROM files WHERE submission_id=?", (submission_id,)).fetchall()
    assert len(rows) == 3
    assert all(bytes(row["content"]) == b"" and row["object_key"] in state.objects for row in rows)
    assert client.get(f"/api/submissions/{submission_id}/csv").status_code == 409
    confirmed = client.post(f"/api/submissions/{submission_id}/confirm", json={"records": submission["records"]})
    assert confirmed.status_code == 200, confirmed.text
    response = client.get(f"/api/submissions/{submission_id}/csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 6
    assert {row["bank"] for row in rows} == {"BPI", "BDO", "UnionBank"}
    assert {row["account_number"] for row in rows} == {"0000123456", "0001123456", "0002123456"}
    listing = client.get("/api/submissions").json()
    assert listing[0]["row_count"] == 6
    detail = client.get(f"/api/submissions/{submission_id}").json()
    file_id = detail["files"][0]["id"]
    original = client.get(f"/api/submissions/{submission_id}/files/{file_id}")
    assert original.status_code == 200
    assert original.content.startswith(b"%PDF-")
    assert state.verified


def test_postgres_owner_isolation_and_admin_settings(remote_client):
    client, state = remote_client
    login(client)
    submission = upload(client, ("BPI",)).json()
    submission_id = submission["id"]
    file_id = client.get(f"/api/submissions/{submission_id}").json()["files"][0]["id"]
    login(client, "other@example.com")
    assert client.get("/api/submissions").json() == []
    for suffix in ["", "/csv", f"/files/{file_id}"]:
        assert client.get(f"/api/submissions/{submission_id}{suffix}").status_code == 404
    assert client.post(f"/api/submissions/{submission_id}/confirm", json={"records": submission["records"]}).status_code == 404
    assert client.delete(f"/api/submissions/{submission_id}").status_code == 404
    assert client.get("/api/admin/settings").status_code == 403
    assert client.put("/api/admin/settings", json={}).status_code == 403
    assert len(state.objects) == 1
    login(client, "admin@example.com", "admin-password-12345", register=False)
    assert len(client.get("/api/submissions").json()) == 1
    assert client.get(f"/api/submissions/{submission_id}/files/{file_id}").status_code == 200
    settings = {"registration_open": False, "max_files": 5, "max_file_mb": 2}
    assert client.put("/api/admin/settings", json=settings).status_code == 200
    assert client.get("/api/admin/settings").json() == settings
    assert client.post("/api/register", json={"name": "Blocked", "email": "blocked@example.com", "password": "blocked-password-123"}).status_code == 403


def test_postgres_restart_preserves_accounts_sessions_submissions_and_settings(remote_state, monkeypatch):
    with TestClient(main.app) as client:
        user = login(client)
        submission = upload(client, ("BDO",)).json()
        submission_id = submission["id"]
        assert client.post(f"/api/submissions/{submission_id}/confirm", json={"records": submission["records"]}).status_code == 200
        admin = login(client, "admin@example.com", "admin-password-12345", register=False)
        assert client.put("/api/admin/settings", json={"registration_open": True, "max_files": 4, "max_file_mb": 3}).status_code == 200
        cookies, csrf = dict(client.cookies), client.headers["x-csrf-token"]
    # Restart must not reset an existing administrator's password from bootstrap env.
    monkeypatch.setenv("ADMIN_PASSWORD", "different-bootstrap-password-6789")
    with TestClient(main.app) as restarted:
        restarted.cookies.update(cookies)
        restarted.headers["x-csrf-token"] = csrf
        session = restarted.get("/api/me")
        assert session.status_code == 200
        assert session.json()["user"]["id"] == admin["id"]
        assert session.json()["settings"]["max_files"] == 4
        existing_user = login(restarted, register=False)
        assert existing_user["id"] == user["id"]
        detail = restarted.get(f"/api/submissions/{submission_id}").json()
        file_id = detail["files"][0]["id"]
        assert restarted.get(f"/api/submissions/{submission_id}/files/{file_id}").content.startswith(b"%PDF-")
        assert restarted.get(f"/api/submissions/{submission_id}/csv").status_code == 200
        login(restarted, "admin@example.com", "admin-password-12345", register=False)
        assert len(restarted.get("/api/submissions").json()) == 1
    assert len(remote_state.verified) == 2


def test_failed_remote_upload_cleans_indeterminate_objects_and_saves_no_submission(remote_client, monkeypatch):
    client, state = remote_client
    login(client)
    attempts = []

    def fail_second_upload(key, content):
        state.objects[key] = content
        attempts.append(key)
        if len(attempts) == 2:
            raise storage.StorageError("Statement storage is unavailable. Please try again later.")

    monkeypatch.setattr(storage, "upload", fail_second_upload)
    result = upload(client, ("BPI", "BDO"))
    assert result.status_code == 503
    assert len(attempts) == 2
    assert set(state.deleted) == set(attempts)
    assert state.objects == {}
    with main.database() as db:
        assert db.execute("SELECT count(*) AS n FROM submissions").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM files").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM storage_cleanup").fetchone()["n"] == 0


def test_failed_remote_cleanup_leaves_durable_intents_without_submissions(remote_client, monkeypatch):
    client, state = remote_client
    login(client)

    def fail_upload_after_commit(key, content):
        state.objects[key] = content
        raise storage.StorageError("Statement storage is unavailable. Please try again later.")

    def fail_cleanup(keys):
        raise storage.StorageError("Statement storage is unavailable. Please try again later.")

    monkeypatch.setattr(storage, "upload", fail_upload_after_commit)
    monkeypatch.setattr(storage, "delete", fail_cleanup)
    result = upload(client, ("BPI",))
    assert result.status_code == 503
    assert len(state.objects) == 1
    with main.database() as db:
        assert db.execute("SELECT count(*) AS n FROM submissions").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM files").fetchone()["n"] == 0
        intents = db.execute("SELECT object_key FROM storage_cleanup").fetchall()
    assert {row["object_key"] for row in intents} == set(state.objects)


def test_remote_delete_failure_preserves_metadata_for_retry(remote_client, monkeypatch):
    client, state = remote_client
    login(client)
    submission = upload(client, ("BPI",)).json()
    submission_id = submission["id"]
    delete_objects = storage.delete

    def fail_delete(keys):
        raise storage.StorageError("Statement storage is unavailable. Please try again later.")

    monkeypatch.setattr(storage, "delete", fail_delete)
    assert client.delete(f"/api/submissions/{submission_id}").status_code == 503
    assert client.get(f"/api/submissions/{submission_id}").status_code == 200
    assert len(state.objects) == 1
    monkeypatch.setattr(storage, "delete", delete_objects)
    assert client.delete(f"/api/submissions/{submission_id}").status_code == 200
    assert client.get(f"/api/submissions/{submission_id}").status_code == 404
    assert state.objects == {}
    with main.database() as db:
        assert db.execute("SELECT count(*) AS n FROM files").fetchone()["n"] == 0


def test_startup_retries_only_aged_unreferenced_cleanup_intents(remote_client):
    client, state = remote_client
    login(client)
    saved = upload(client, ("BPI",)).json()
    active_key = next(iter(state.objects))
    orphan, recent = "orphan/stale.pdf", "orphan/recent.pdf"
    state.objects[orphan] = b"stale original"
    state.objects[recent] = b"recent request original"
    with main.database() as db:
        for key, created_at in [(orphan, int(time.time()) - 7200), (recent, int(time.time())),
                                (active_key, int(time.time()) - 7200)]:
            db.execute("INSERT INTO storage_cleanup VALUES (?,?)", (key, created_at))
    main.initialize()
    assert orphan not in state.objects
    assert recent in state.objects
    assert active_key in state.objects
    assert state.deleted == [orphan]
    assert client.get(f"/api/submissions/{saved['id']}").status_code == 200
    with main.database() as db:
        pending = db.execute("SELECT object_key FROM storage_cleanup").fetchall()
    assert {row["object_key"] for row in pending} == {recent, active_key}


def test_lost_commit_acknowledgment_does_not_delete_committed_originals(remote_state, monkeypatch):
    state = remote_state
    with TestClient(main.app, raise_server_exceptions=False) as client:
        login(client)
        original_database = main.database

        @contextmanager
        def ambiguous_commit():
            inserted = False
            with original_database() as db:
                class TrackInsert:
                    def __getattr__(self, name):
                        return getattr(db, name)

                    def execute(self, query, parameters=()):
                        nonlocal inserted
                        if query.startswith("INSERT INTO submissions"):
                            inserted = True
                        return db.execute(query, parameters)

                yield TrackInsert()
            if inserted:
                # The transaction already committed, but the client did not get
                # the acknowledgement. Compensation must re-read durable intent.
                raise RuntimeError("Synthetic lost commit acknowledgment")

        monkeypatch.setattr(main, "database", ambiguous_commit)
        assert upload(client, ("BPI",)).status_code == 500
        listing = client.get("/api/submissions").json()
        assert len(listing) == 1
        assert len(state.objects) == 1
        assert state.deleted == []
        detail = client.get(f"/api/submissions/{listing[0]['id']}").json()
        file_id = detail["files"][0]["id"]
        assert client.get(f"/api/submissions/{listing[0]['id']}/files/{file_id}").content.startswith(b"%PDF-")
        with main.database() as db:
            assert db.execute("SELECT count(*) AS n FROM storage_cleanup").fetchone()["n"] == 0


def test_postgres_tables_have_private_schema_and_row_level_security(remote_client):
    _, state = remote_client
    with psycopg.connect(state.url, row_factory=dict_row) as connection:
        rows = connection.execute("""
          SELECT c.relname,c.relrowsecurity FROM pg_class c
          JOIN pg_namespace n ON c.relnamespace=n.oid
          WHERE n.nspname='ledger_private' AND c.relkind='r'
        """).fetchall()
        assert {row["relname"] for row in rows} == set(persistence.TABLES)
        assert all(row["relrowsecurity"] for row in rows)
        public_grants = connection.execute("""
          SELECT a.privilege_type FROM pg_namespace n
          CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a
          WHERE n.nspname='ledger_private' AND a.grantee=0
        """).fetchall()
        assert public_grants == []
        for name in ["anon", "authenticated"]:
            role = connection.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
            if role:
                privileges = connection.execute("SELECT has_schema_privilege(%s,'ledger_private','USAGE') AS allowed", (role["oid"],)).fetchone()
                assert privileges["allowed"] is False


@pytest.mark.parametrize("missing", ["DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"])
def test_incomplete_remote_startup_fails_before_local_database(monkeypatch, tmp_path, missing):
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    values = {"DATABASE_URL": "postgresql://user:test-password@localhost/test?sslmode=disable",
              "SUPABASE_URL": "https://synthetic-project.supabase.co",
              "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_synthetic-test-only"}
    for key, value in values.items():
        if key != missing:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "must-not-exist.sqlite3")

    def unexpected_sqlite(*args, **kwargs):
        pytest.fail("Remote startup must not fall back to SQLite.")

    monkeypatch.setattr(persistence.sqlite3, "connect", unexpected_sqlite)
    with pytest.raises(RuntimeError, match="requires DATABASE_URL"):
        with TestClient(main.app):
            pass
    assert not main.DB_PATH.exists()


def test_remote_credentials_require_explicit_remote_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:test-password@localhost/test")
    with pytest.raises(RuntimeError, match="Refusing a local-storage fallback"):
        with persistence.database(tmp_path / "must-not-exist.sqlite3"):
            pass
    assert not (tmp_path / "must-not-exist.sqlite3").exists()


@pytest.mark.parametrize("database_url", [
    "sqlite:///tmp/database.sqlite3", "https://database.example.invalid",
    "postgresql://user:test-password@db.example.invalid/database?sslmode=disable",
])
def test_invalid_remote_database_configuration_hides_credentials(monkeypatch, database_url):
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SUPABASE_URL", "https://synthetic-project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-test-only")
    with pytest.raises(RuntimeError) as error:
        persistence.remote()
    assert "test-password" not in str(error.value)


def test_remote_connection_failure_never_creates_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:test-password@localhost/test?sslmode=disable")
    monkeypatch.setenv("SUPABASE_URL", "https://synthetic-project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-test-only")

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("Synthetic database outage")

    def unexpected_sqlite(*args, **kwargs):
        pytest.fail("A remote outage must not fall back to SQLite.")

    monkeypatch.setattr(persistence.psycopg, "connect", unavailable)
    monkeypatch.setattr(persistence.sqlite3, "connect", unexpected_sqlite)
    with pytest.raises(psycopg.OperationalError, match="Synthetic database outage"):
        with persistence.database(tmp_path / "must-not-exist.sqlite3"):
            pass
    assert not (tmp_path / "must-not-exist.sqlite3").exists()


@pytest.mark.parametrize("url", [
    "postgresql://test:synthetic-password@production.supabase.co/test",
    "postgresql://test:synthetic-password@localhost/test?hostaddr=203.0.113.1",
    "postgresql://test:synthetic-password@127.0.0.1/test?host=production.supabase.co",
    "postgresql://test:synthetic-password@localhost/test?service=production",
    "postgresql:///test",
])
def test_schema_cleanup_refuses_nonlocal_connection_options(url):
    with pytest.raises(pytest.fail.Exception, match="loopback"):
        require_local_test_database(url)
