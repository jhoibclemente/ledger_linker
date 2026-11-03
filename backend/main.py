"""Same-origin API and web UI. All authorization is enforced in the API."""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from backend.parser import BANKS, SCHEMA, StatementError, extract_statement, normalize_record

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "ledger.sqlite3"
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "true").lower() == "true"
SESSION_AGE = 8 * 60 * 60
MAX_BODY = 52 * 1024 * 1024
DEFAULT_SETTINGS = {"registration_open": True, "max_files": 10, "max_file_mb": 10}
PARSERS = threading.BoundedSemaphore(1)
@asynccontextmanager
async def lifespan(application):
    initialize()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ":" + digest


def password_valid(password, encoded):
    return hmac.compare_digest(password_hash(password, encoded.split(":")[0]), encoded)


def validate_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise HTTPException(400, "Use a password with 12–128 characters.")


def email_address(value):
    value = str(value).strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise HTTPException(400, "Enter a valid email address.")
    return value


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with database() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','admin')),
          created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          csrf TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS submissions (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
          title TEXT NOT NULL, status TEXT NOT NULL, records TEXT NOT NULL, file_count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS files (
          id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
          filename TEXT NOT NULL, content BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS submissions_owner ON submissions(user_id, created_at);
        """)
        db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (json.dumps(DEFAULT_SETTINGS),))
        admin_email = os.environ.get("ADMIN_EMAIL")
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if admin_email and admin_password and not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            validate_password(admin_password)
            email = email_address(admin_email)
            if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise RuntimeError("Bootstrap email already belongs to a user; use the offline admin CLI.")
            db.execute("INSERT INTO users VALUES (?, ?, ?, ?, 'admin', ?)",
                       (secrets.token_hex(16), email, "Administrator", password_hash(admin_password), int(time.time())))
    os.chmod(DB_PATH, 0o600)


class BodyLimit:
    def __init__(self, application):
        self.app = application

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        count = 0

        async def limited_receive():
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > MAX_BODY:
                raise HTTPException(413, "Combined request exceeds 52 MB. Upload a smaller batch.")
            return message
        await self.app(scope, limited_receive, send)


app.add_middleware(BodyLimit)


@app.exception_handler(json.JSONDecodeError)
async def invalid_json(request, error):
    return JSONResponse({"detail": "The request must contain valid JSON."}, status_code=400)


async def payload(request):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Use application/json for this request.")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "The request must be a JSON object.")
    return body


def bounded_extract(content, filename, bank, account, year, order):
    if not PARSERS.acquire(blocking=False):
        raise HTTPException(503, "The converter is busy. Please try again shortly.")
    try:
        data = {"data": base64.b64encode(content).decode(), "filename": filename,
                "bank": bank, "account": account, "year": year, "order": order}
        try:
            result = subprocess.run([sys.executable, "-m", "backend.extract_worker"],
                input=json.dumps(data), capture_output=True, text=True, timeout=40,
                cwd=Path(__file__).resolve().parent.parent)
            output = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            raise StatementError("This PDF exceeded processing limits. Try a smaller statement.")
        if "error" in output:
            raise StatementError(output["error"])
        return output["records"]
    finally:
        PARSERS.release()


@app.middleware("http")
async def security_headers(request, call_next):
    # Origin validation covers login/registration too; session mutations also need CSRF.
    origin = request.headers.get("origin")
    expected = os.environ.get("APP_ORIGIN") or str(request.base_url).rstrip("/")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != expected:
        return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
    length = request.headers.get("content-length", "0")
    if not length.isdigit() or int(length) > MAX_BODY:
        return JSONResponse({"detail": "Request exceeds the 52 MB batch limit."}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers["Cache-Control"] = "no-store"
    if SECURE_COOKIES:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def rate_limit(request, category, identity="", limit=10, seconds=900):
    # Trust no client-supplied forwarding header. Account buckets also prevent
    # distributed password guessing; IP buckets protect registration and upload.
    ip = request.client.host if request.client else "unknown"
    keys = [f"{category}:ip:{ip}"]
    if identity:
        keys.append(f"{category}:account:{hashlib.sha256(identity.encode()).hexdigest()}")
    now = int(time.time())
    with database() as db:
        db.execute("DELETE FROM rate_limits WHERE expires < ?", (now,))
        for key in keys:
            row = db.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
            if row and row["attempts"] >= limit:
                raise HTTPException(429, "Too many attempts. Please try again later.")
        for key in keys:
            db.execute("INSERT INTO rate_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1", (key, now + seconds))


def current_user(request, admin=False):
    token = request.cookies.get("ledger_session", "")
    with database() as db:
        row = db.execute("SELECT users.*, sessions.csrf FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>?",
                         (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
    if not row:
        raise HTTPException(401, "Please sign in.")
    if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf"]):
        raise HTTPException(403, "Session verification failed. Refresh and sign in again.")
    if admin and row["role"] != "admin":
        raise HTTPException(403, "Administrator access required.")
    return dict(row)


def public_user(user):
    return {key: user[key] for key in ["id", "email", "name", "role"]}


def settings():
    with database() as db:
        return json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])


@app.get("/api/health")
def health():
    return {"status": "ok", "registration_open": settings()["registration_open"]}


@app.post("/api/register")
async def register(request: Request):
    rate_limit(request, "register", limit=5, seconds=3600)
    if not settings()["registration_open"]:
        raise HTTPException(403, "Registration is currently closed. Contact your administrator.")
    body = await payload(request)
    email = email_address(body.get("email", ""))
    password = body.get("password", "")
    validate_password(password)
    name = str(body.get("name", "")).strip()
    if not 1 <= len(name) <= 100:
        raise HTTPException(400, "Enter a name of 1–100 characters.")
    try:
        with database() as db:
            db.execute("INSERT INTO users VALUES (?, ?, ?, ?, 'user', ?)",
                       (secrets.token_hex(16), email, name, password_hash(password), int(time.time())))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account already exists for this email. Please sign in.")
    return {"message": "Account created. Sign in to continue."}


@app.post("/api/login")
async def login(request: Request):
    body = await payload(request)
    email = email_address(body.get("email", ""))
    rate_limit(request, "login", email)
    password = body.get("password", "")
    if not isinstance(password, str) or len(password) > 128:
        raise HTTPException(400, "Invalid password.")
    with database() as db:
        row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    encoded = row["password"] if row else password_hash("unused-password-padding")
    valid = await run_in_threadpool(password_valid, password, encoded)
    if not valid or not row:
        raise HTTPException(401, "Email or password is incorrect.")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with database() as db:
        db.execute("DELETE FROM sessions WHERE expires<?", (int(time.time()),))
        old = request.cookies.get("ledger_session", "")
        db.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(old.encode()).hexdigest(),))
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), row["id"], csrf, int(time.time()) + SESSION_AGE))
    response = JSONResponse({"user": public_user(row), "csrf": csrf})
    response.set_cookie("ledger_session", token, max_age=SESSION_AGE, httponly=True, secure=SECURE_COOKIES, samesite="strict", path="/")
    return response


@app.get("/api/me")
def me(request: Request):
    user = current_user(request)
    return {"user": public_user(user), "csrf": user["csrf"], "settings": settings()}


@app.post("/api/logout")
def logout(request: Request):
    current_user(request)
    with database() as db:
        token = hashlib.sha256(request.cookies.get("ledger_session", "").encode()).hexdigest()
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
    response = JSONResponse({"message": "Signed out."})
    response.delete_cookie("ledger_session", path="/", secure=SECURE_COOKIES, httponly=True, samesite="strict")
    return response


@app.get("/api/submissions")
def list_submissions(request: Request):
    user = current_user(request)
    clause, params = ("", ()) if user["role"] == "admin" else ("WHERE s.user_id=?", (user["id"],))
    with database() as db:
        rows = db.execute(f"SELECT s.id,s.created_at,s.title,s.status,s.file_count,json_array_length(s.records) AS row_count,u.email AS owner FROM submissions s JOIN users u ON s.user_id=u.id {clause} ORDER BY s.created_at DESC LIMIT 500", params).fetchall()
    return [dict(row) for row in rows]


def authorized_submission(user, submission_id):
    with database() as db:
        row = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
    if not row or (user["role"] != "admin" and row["user_id"] != user["id"]):
        raise HTTPException(404, "Submission not found.")
    return dict(row)


@app.post("/api/submissions")
async def upload(request: Request):
    user = current_user(request)
    rate_limit(request, "upload", user["id"], limit=20, seconds=3600)
    limits = settings()
    with database() as db:
        if db.execute("SELECT count(*) FROM submissions WHERE user_id=?", (user["id"],)).fetchone()[0] >= 100:
            raise HTTPException(400, "You have reached 100 submissions. Delete older submissions before uploading.")
    try:
        async with request.form(max_files=10, max_fields=5, max_part_size=65536) as form:
            files = form.getlist("files")
            metadata = json.loads(str(form.get("metadata", "[]")))
            if not isinstance(metadata, list) or not 1 <= len(files) <= limits["max_files"] or len(files) != len(metadata):
                raise HTTPException(400, "Choose PDFs and complete the details for each file.")
            records, originals = [], []
            total = 0
            for index, file in enumerate(files):
                if not hasattr(file, "read") or not str(file.filename).lower().endswith(".pdf"):
                    raise HTTPException(400, "Only PDF files are accepted.")
                filename = re.sub(r"[^\w.() -]", "_", Path(file.filename).name)[:150]
                if filename in {name for name, _ in originals}:
                    raise HTTPException(400, "Use distinct filenames for each PDF in a batch.")
                content = await file.read(limits["max_file_mb"] * 1024 * 1024 + 1)
                total += len(content)
                if len(content) > limits["max_file_mb"] * 1024 * 1024 or total > 50 * 1024 * 1024:
                    raise HTTPException(413, "Upload exceeds the per-file limit or 50 MB batch limit.")
                meta = metadata[index]
                if not isinstance(meta, dict):
                    raise HTTPException(400, "Invalid file details.")
                year = int(meta["year"]) if meta.get("year") else None
                try:
                    extracted = await run_in_threadpool(bounded_extract, content, filename,
                        str(meta.get("bank", "")), str(meta.get("account", "")).strip(), year, str(meta.get("order", "MDY")))
                except StatementError as exc:
                    raise HTTPException(422, f"{filename}: {exc}") from exc
                records.extend(extracted)
                if len(records) > 10000:
                    raise HTTPException(400, "A batch may contain at most 10,000 transactions.")
                originals.append((filename, content))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "Invalid file details. Check the selected bank, account and year.") from exc
    submission_id = secrets.token_hex(16)
    if shutil.disk_usage(DATA_DIR).free < total + 100 * 1024 * 1024:
        raise HTTPException(507, "Storage is nearly full. Ask your administrator to free space or expand the disk.")
    title = str(form.get("title", "Statement batch")).strip()[:100] or "Statement batch"
    with database() as db:
        db.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?)", (submission_id, user["id"], int(time.time()), title, "draft", json.dumps(records), len(originals)))
        for filename, content in originals:
            db.execute("INSERT INTO files VALUES (?,?,?,?)", (secrets.token_hex(16), submission_id, filename, content))
    return {"id": submission_id, "records": records}


@app.get("/api/submissions/{submission_id}")
def detail(submission_id: str, request: Request):
    submission = authorized_submission(current_user(request), submission_id)
    submission["records"] = json.loads(submission["records"])
    with database() as db:
        submission["files"] = [dict(row) for row in db.execute("SELECT id,filename FROM files WHERE submission_id=?", (submission_id,)).fetchall()]
    return submission


@app.post("/api/submissions/{submission_id}/confirm")
async def confirm(submission_id: str, request: Request):
    submission = authorized_submission(current_user(request), submission_id)
    body = await payload(request)
    records = body.get("records")
    original = json.loads(submission["records"])
    if submission["status"] != "draft":
        raise HTTPException(409, "This submission is already finalized. Upload a new batch for changes.")
    if not isinstance(records, list) or len(records) != len(original):
        raise HTTPException(400, "Keep every extracted transaction. Review and correct the values before confirming.")
    normalized = []
    try:
        for row, source in zip(records, original):
            clean = normalize_record(row)
            if any(clean[key] != source[key] for key in ["bank", "account_number", "source_file", "currency"]):
                raise StatementError("Source bank, account, file and currency cannot change during review. Re-upload with corrected file details.")
            normalized.append(clean)
    except (StatementError, TypeError, AttributeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    with database() as db:
        result = db.execute("UPDATE submissions SET records=?,status='ready' WHERE id=? AND status='draft'", (json.dumps(normalized), submission_id))
        if result.rowcount != 1:
            raise HTTPException(409, "Submission was already confirmed. Refresh the page.")
    return {"message": "Submission confirmed. CSV is ready."}


def csv_cell(value):
    # Spreadsheet formula protection also applies to attacker-controlled filenames.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


@app.get("/api/submissions/{submission_id}/csv")
def download_csv(submission_id: str, request: Request):
    submission = authorized_submission(current_user(request), submission_id)
    if submission["status"] != "ready":
        raise HTTPException(409, "Review and confirm this submission before downloading.")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=SCHEMA)
    writer.writeheader()
    rows = sorted(json.loads(submission["records"]), key=lambda row: (row["bank"], row["account_number"], row["transaction_date"], row["source_file"]))
    for row in rows:
        writer.writerow({key: csv_cell(value) if key in {"description", "source_file", "account_number"} else value for key, value in row.items()})
    return Response("\ufeff" + output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="ledger-{submission_id[:8]}.csv"'})


@app.get("/api/submissions/{submission_id}/files/{file_id}")
def download_pdf(submission_id: str, file_id: str, request: Request):
    authorized_submission(current_user(request), submission_id)
    with database() as db:
        row = db.execute("SELECT * FROM files WHERE id=? AND submission_id=?", (file_id, submission_id)).fetchone()
    if not row:
        raise HTTPException(404, "File not found.")
    return Response(row["content"], media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="statement.pdf"'})


@app.delete("/api/submissions/{submission_id}")
def delete_submission(submission_id: str, request: Request):
    authorized_submission(current_user(request), submission_id)
    with database() as db:
        db.execute("DELETE FROM submissions WHERE id=?", (submission_id,))
    return {"message": "Submission and original PDFs deleted."}


@app.get("/api/admin/settings")
def get_settings(request: Request):
    current_user(request, admin=True)
    return settings()


@app.put("/api/admin/settings")
async def update_settings(request: Request):
    current_user(request, admin=True)
    body = await payload(request)
    if set(body) != set(DEFAULT_SETTINGS) or type(body["registration_open"]) is not bool or type(body["max_files"]) is not int or type(body["max_file_mb"]) is not int:
        raise HTTPException(400, "Invalid settings.")
    if not 1 <= body["max_files"] <= 10 or not 1 <= body["max_file_mb"] <= 20:
        raise HTTPException(400, "Allow 1–10 files per batch and 1–20 MB per file.")
    with database() as db:
        db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(body),))
    return body


@app.post("/api/account/password")
async def change_password(request: Request):
    user = current_user(request)
    body = await payload(request)
    rate_limit(request, "password", user["id"], limit=5)
    previous = body.get("current_password", "")
    if not isinstance(previous, str) or len(previous) > 128 or not password_valid(previous, user["password"]):
        raise HTTPException(400, "Current password is incorrect.")
    validate_password(body.get("new_password"))
    with database() as db:
        db.execute("UPDATE users SET password=? WHERE id=?", (password_hash(body["new_password"]), user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    response = JSONResponse({"message": "Password changed. Sign in again."})
    response.delete_cookie("ledger_session", path="/")
    return response


@app.get("/")
def root():
    return RedirectResponse("/app/")


app.mount("/app", StaticFiles(directory=Path(__file__).resolve().parent.parent / "app", html=True), name="app")
