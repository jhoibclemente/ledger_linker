import json

import httpx
import pytest

from backend import storage


@pytest.fixture(autouse=True)
def clear_storage_environment(monkeypatch):
    for name in ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_BUCKET"]:
        monkeypatch.delenv(name, raising=False)


def test_absent_configuration_uses_local_storage():
    assert storage.configured() is False


@pytest.mark.parametrize("name,value", [
    ("SUPABASE_URL", "https://project.supabase.co"),
    ("SUPABASE_SERVICE_ROLE_KEY", "test-secret"),
])
def test_partial_configuration_fails(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="Configure both"):
        storage.configured()


@pytest.mark.parametrize("url", ["http://project.supabase.co", "https://name:password@project.supabase.co",
                                  "https://project.supabase.co/storage", "https://project.supabase.co?key=bad",
                                  "https://project.supabase.co:invalid", "https://[invalid"])
def test_unsafe_project_origins_are_rejected(url):
    with pytest.raises(RuntimeError, match="HTTPS project origin"):
        storage.SupabaseStorage(url, "test-secret")


def test_private_bucket_upload_download_and_delete():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["apikey"] == "legacy-test-service-jwt"
        assert request.headers["authorization"] == "Bearer legacy-test-service-jwt"
        if request.url.path == "/storage/v1/bucket/ledger-statements":
            return httpx.Response(200, json={"id": "ledger-statements", "public": False})
        if request.method == "POST":
            assert request.url.path == "/storage/v1/object/ledger-statements/submission/file.pdf"
            assert request.headers["content-type"] == "application/pdf"
            assert request.headers["x-upsert"] == "false"
            assert request.content == b"%PDF-synthetic-test"
            return httpx.Response(200, json={"Key": "submission/file.pdf"})
        if request.method == "GET":
            assert request.url.path == "/storage/v1/object/authenticated/ledger-statements/submission/file.pdf"
            return httpx.Response(200, content=b"%PDF-synthetic-test")
        assert request.method == "DELETE"
        assert request.url.path == "/storage/v1/object/ledger-statements"
        assert json.loads(request.content) == {"prefixes": ["submission/file.pdf"]}
        return httpx.Response(200, json=[])

    store = storage.SupabaseStorage("https://project.supabase.co/", "legacy-test-service-jwt", transport=httpx.MockTransport(handle))
    store.ensure_private_bucket()
    store.upload("submission/file.pdf", b"%PDF-synthetic-test")
    assert store.download("submission/file.pdf") == b"%PDF-synthetic-test"
    store.delete(["submission/file.pdf"])
    store.delete([])
    assert len(calls) == 4


def test_opaque_secret_key_is_not_used_as_a_jwt():
    def handle(request):
        assert request.headers["apikey"] == "sb_secret_synthetic-test"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"public": False})
    store = storage.SupabaseStorage("https://project.supabase.co", "sb_secret_synthetic-test", transport=httpx.MockTransport(handle))
    store.ensure_private_bucket()


@pytest.mark.parametrize("metadata", [{"public": True}, {}, [], {"public": "false"}])
def test_public_or_unknown_bucket_is_rejected(metadata):
    store = storage.SupabaseStorage("https://project.supabase.co", "test-secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=metadata)))
    with pytest.raises(storage.StorageError, match="must be private"):
        store.ensure_private_bucket()


@pytest.mark.parametrize("status", [301, 401, 403, 404, 500])
def test_provider_errors_are_generic_and_redirects_not_followed(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "sensitive-provider-detail"}, headers={"location": "https://elsewhere.invalid"})

    store = storage.SupabaseStorage("https://project.supabase.co", "sensitive-test-key", transport=httpx.MockTransport(handle))
    with pytest.raises(storage.StorageError) as error:
        store.download("submission/file.pdf")
    assert "sensitive" not in str(error.value)
    assert error.value.__suppress_context__
    assert len(calls) == 1


def test_transport_failure_has_no_provider_exception_chain():
    def handle(request):
        raise httpx.ConnectError("provider connection details", request=request)
    store = storage.SupabaseStorage("https://project.supabase.co", "test-secret", transport=httpx.MockTransport(handle))
    with pytest.raises(storage.StorageError) as error:
        store.download("submission/file.pdf")
    assert "provider connection" not in str(error.value)
    assert error.value.__suppress_context__


def test_object_paths_are_encoded():
    def handle(request):
        assert request.url.raw_path == b"/storage/v1/object/authenticated/ledger-statements/submission/a%20file%3F.pdf"
        return httpx.Response(200, content=b"pdf")
    store = storage.SupabaseStorage("https://project.supabase.co", "test-secret", transport=httpx.MockTransport(handle))
    assert store.download("submission/a file?.pdf") == b"pdf"


@pytest.mark.parametrize("key", ["", "/absolute.pdf", "../escape.pdf", "submission/../escape.pdf", "submission//file.pdf"])
def test_invalid_object_keys_are_rejected(key):
    store = storage.SupabaseStorage("https://project.supabase.co", "test-secret")
    with pytest.raises(ValueError, match="relative path"):
        store.download(key)
