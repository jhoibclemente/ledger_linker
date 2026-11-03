"""Server-only access to a private Supabase Storage bucket.

Authorization to individual submissions belongs to the application API. The
service key is deliberately never sent to browsers, stored in object metadata,
included in exception messages, or placed in object URLs.
"""
import os
import re
from urllib.parse import quote, urlsplit

import httpx


class StorageError(RuntimeError):
    """Safe to describe to a user without exposing provider responses."""


def _validate(url, key, bucket):
    if not url or not key:
        raise RuntimeError("Configure both SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
    try:
        parsed = urlsplit(url)
        parsed.port  # Validate any explicit port without including it in errors.
    except ValueError:
        raise RuntimeError("SUPABASE_URL must be a valid HTTPS project origin.") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise RuntimeError("SUPABASE_URL must be the HTTPS project origin, without credentials or a path.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", bucket):
        raise RuntimeError("SUPABASE_BUCKET must be a bucket ID containing letters, digits, dots, underscores or hyphens.")


def _environment():
    return (os.environ.get("SUPABASE_URL", "").strip(),
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
            os.environ.get("SUPABASE_BUCKET", "ledger-statements").strip())


def configured():
    """Distinguish local storage from an invalid partial remote configuration."""
    url, key, bucket = _environment()
    if not url and not key:
        return False
    _validate(url, key, bucket)
    return True


def _object_path(key):
    if (not isinstance(key, str) or not key or key.startswith("/") or
            any(part in {"", ".", ".."} for part in key.split("/"))):
        raise ValueError("Storage object key must be a nonempty relative path.")
    return quote(key, safe="/")


class SupabaseStorage:
    def __init__(self, url, key, bucket="ledger-statements", *, transport=None):
        _validate(url, key, bucket)
        self.url = url.rstrip("/")
        self.bucket = bucket
        self._key = key
        self._transport = transport

    def _request(self, method, path, **kwargs):
        headers = {"apikey": self._key}
        # Supabase's opaque secret keys belong in apikey, not in a JWT Bearer
        # header. Legacy service_role JWTs support both headers.
        if not self._key.startswith("sb_secret_"):
            headers["Authorization"] = "Bearer " + self._key
        try:
            with httpx.Client(base_url=self.url, headers=headers,
                              timeout=httpx.Timeout(30.0, connect=5.0),
                              follow_redirects=False, transport=self._transport) as client:
                response = client.request(method, "/storage/v1/" + path, **kwargs)
                response.raise_for_status()
                return response
        except httpx.HTTPError:
            # Provider response bodies and exception chains are intentionally
            # omitted: they can contain project details or request metadata.
            raise StorageError("Statement storage is unavailable. Please try again later.") from None

    def ensure_private_bucket(self):
        response = self._request("GET", "bucket/" + quote(self.bucket, safe=""))
        try:
            metadata = response.json()
        except ValueError:
            raise StorageError("The statement storage bucket could not be verified.") from None
        if not isinstance(metadata, dict) or metadata.get("public") is not False:
            raise StorageError("The configured statement storage bucket must be private.")

    def upload(self, key, content):
        self._request("POST", "object/" + quote(self.bucket, safe="") + "/" + _object_path(key),
                      content=content, headers={"Content-Type": "application/pdf", "x-upsert": "false"})

    def download(self, key):
        return self._request("GET", "object/authenticated/" + quote(self.bucket, safe="") + "/" + _object_path(key)).content

    def delete(self, keys):
        if not keys:
            return
        for key in keys:
            _object_path(key)
        self._request("DELETE", "object/" + quote(self.bucket, safe=""), json={"prefixes": keys})


def _store():
    if not configured():
        raise StorageError("Remote statement storage is not configured.")
    return SupabaseStorage(*_environment())


def ensure_private_bucket():
    _store().ensure_private_bucket()


def upload(key, content):
    _store().upload(key, content)


def download(key):
    return _store().download(key)


def delete(keys):
    _store().delete(keys)
