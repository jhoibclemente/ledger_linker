import csv
import io
import json

import pytest

from backend.parser import StatementError, extract_statement, normalize_date
from tests.conftest import login, statement_pdf, upload


@pytest.mark.parametrize("value,year,order,expected", [
    ("11/02/2026", None, "MDY", "2026-11-02"), ("02/11/2026", None, "DMY", "2026-11-02"),
    ("02-Nov-2026", None, "MDY", "2026-11-02"), ("Nov 02 2026", None, "MDY", "2026-11-02"),
    ("11/02", 2026, "MDY", "2026-11-02"), ("2026-11-02", None, "DMY", "2026-11-02"),
    ("12/31/2025", None, "MDY", "2025-12-31"), ("01/01/2026", None, "MDY", "2026-01-01"),
])
def test_dates(value, year, order, expected):
    assert normalize_date(value, year, order) == expected


@pytest.mark.parametrize("value", ["02/30/2026", "11/02", "not-a-date"])
def test_invalid_or_missing_year(value):
    with pytest.raises(StatementError):
        normalize_date(value)


@pytest.mark.parametrize("bank,headers", [
    ("BPI", ["Date", "Description", "Debit", "Credit", "Balance"]),
    ("BDO", ["Date", "Particulars", "Withdrawals", "Deposits", "Balance"]),
    ("UnionBank", ["Date", "Details", "Withdrawal", "Deposit", "Balance"]),
])
def test_bank_column_templates(bank, headers):
    rows = extract_statement(statement_pdf(bank, headers=headers), "statement.pdf", bank)
    assert len(rows) == 2
    assert rows[0]["account_number"] == "0012345678"
    assert rows[1]["transaction_date"] == "2026-11-02"
    assert rows[1]["debit"] == "150.25"


def test_unreconciled_statement_rejected():
    content = statement_pdf(rows=[["11/01/2026", "Deposit", "", "100.00", "100.00"], ["11/02/2026", "Spend", "50.00", "", "75.00"]])
    with pytest.raises(StatementError, match="does not reconcile"):
        extract_statement(content, "bad.pdf", "BPI")


def test_right_aligned_amounts():
    content = statement_pdf(right_aligned=True, rows=[["11/01/2026", "Deposit", "", "123,456.78", "123,456.78"], ["11/02/2026", "Spend", "23,456.78", "", "100,000.00"]])
    rows = extract_statement(content, "aligned.pdf", "BPI")
    assert rows[0]["credit"] == "123456.78"
    assert rows[1]["debit"] == "23456.78"


def test_missing_year_rollover_rejected():
    content = statement_pdf(rows=[["12/31", "Deposit", "", "100.00", "100.00"], ["01/01", "Spend", "50.00", "", "50.00"]])
    with pytest.raises(StatementError, match="January and December"):
        extract_statement(content, "rollover.pdf", "BPI", year=2026)


def test_wrong_bank_or_account_rejected():
    content = statement_pdf()
    with pytest.raises(StatementError, match="does not identify"):
        extract_statement(content, "wrong.pdf", "BDO")
    with pytest.raises(StatementError, match="does not match"):
        extract_statement(content, "wrong.pdf", "BPI", account="9999999999")


def test_summary_rows_do_not_become_transactions():
    content = statement_pdf(rows=[["11/01/2026", "Deposit", "", "100.00", "100.00"], ["Total", "", "0.00", "100.00", "100.00"]])
    assert len(extract_statement(content, "summary.pdf", "BPI")) == 1


def test_unknown_columns_reject_whole_statement():
    with pytest.raises(StatementError, match="columns could not be identified"):
        extract_statement(statement_pdf(headers=["Date", "Description", "Amount", "Type", "Balance"]), "unknown.pdf", "BPI")


def test_aggregate_csv_and_originals(client):
    user = login(client)
    assert user["role"] == "user"  # Submitted role never elevates a registration.
    result = upload(client)
    assert result.status_code == 200, result.text
    data = result.json(); submission_id = data["id"]
    assert len(data["records"]) == 6
    assert client.get(f"/api/submissions/{submission_id}/csv").status_code == 409
    data["records"][0]["description"] = '=HYPERLINK("evil")'
    response = client.post(f"/api/submissions/{submission_id}/confirm", json={"records": data["records"]})
    assert response.status_code == 200, response.text
    csv_response = client.get(f"/api/submissions/{submission_id}/csv")
    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    assert len(rows) == 6
    assert {row["bank"] for row in rows} == {"BPI", "BDO", "UnionBank"}
    assert {row["account_number"] for row in rows} == {"0000123456", "0001123456", "0002123456"}
    assert any(row["description"].startswith("'=") for row in rows)
    detail = client.get(f"/api/submissions/{submission_id}").json()
    assert len(detail["files"]) == 3
    original = client.get(f"/api/submissions/{submission_id}/files/{detail['files'][0]['id']}")
    assert original.content.startswith(b"%PDF-")


def test_user_isolation_and_admin_access(client):
    login(client)
    first = upload(client).json()
    first_id = first["id"]
    detail = client.get(f"/api/submissions/{first_id}").json()
    file_id = detail["files"][0]["id"]
    login(client, "other@example.com")
    assert client.get("/api/submissions").json() == []
    for suffix in ["", "/csv", f"/files/{file_id}"]:
        assert client.get(f"/api/submissions/{first_id}{suffix}").status_code == 404
    assert client.delete(f"/api/submissions/{first_id}").status_code == 404
    assert client.post(f"/api/submissions/{first_id}/confirm", json={"records": first["records"]}).status_code == 404
    assert client.get("/api/admin/settings").status_code == 403
    assert client.put("/api/admin/settings", json={}).status_code == 403
    login(client, "admin@example.com", "admin-password-12345", register=False)
    assert len(client.get("/api/submissions").json()) == 1
    assert client.get(f"/api/submissions/{first_id}").status_code == 200
    assert client.get(f"/api/submissions/{first_id}/files/{file_id}").status_code == 200
    assert client.post(f"/api/submissions/{first_id}/confirm", json={"records": first["records"]}).status_code == 200
    assert client.get(f"/api/submissions/{first_id}/csv").status_code == 200


def test_csrf_logout_password_and_origin(client):
    login(client)
    token = client.headers.pop("x-csrf-token")
    assert client.post("/api/logout").status_code == 403
    client.headers["x-csrf-token"] = token
    assert client.post("/api/logout", headers={"origin": "https://evil.example"}).status_code == 403
    response = client.post("/api/account/password", json={"current_password": "user-password-12345", "new_password": "changed-password-67890"})
    assert response.status_code == 200
    assert client.get("/api/submissions").status_code == 401
    assert client.post("/api/login", json={"email": "user@example.com", "password": "user-password-12345"}).status_code == 401
    login(client, password="changed-password-67890", register=False)
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_admin_settings_and_upload_limits(client):
    login(client, "admin@example.com", "admin-password-12345", register=False)
    settings = {"registration_open": False, "max_files": 1, "max_file_mb": 1}
    assert client.put("/api/admin/settings", json=settings).status_code == 200
    assert client.get("/api/admin/settings").json() == settings
    assert client.post("/api/register", json={"name": "Blocked", "email": "blocked@example.com", "password": "some-password-12345"}).status_code == 403
    assert upload(client).status_code == 400
    assert client.put("/api/admin/settings", json={**settings, "max_files": 11}).status_code == 400


def test_invalid_pdf_is_atomic(client):
    login(client)
    response = client.post("/api/submissions", files=[("files", ("fake.pdf", b"not a PDF", "application/pdf"))],
                           data={"metadata": json.dumps([{"bank": "BPI"}])})
    assert response.status_code == 422
    assert client.get("/api/submissions").json() == []


def test_cannot_change_source_or_remove_rows(client):
    login(client)
    data = upload(client, ("BPI",)).json()
    endpoint = f"/api/submissions/{data['id']}/confirm"
    assert client.post(endpoint, json={"records": data["records"][:1]}).status_code == 400
    data["records"][0]["account_number"] = "999999"
    assert client.post(endpoint, json={"records": data["records"]}).status_code == 400


def test_delete_removes_originals(client):
    login(client)
    data = upload(client, ("BPI",)).json()
    assert client.delete(f"/api/submissions/{data['id']}").status_code == 200
    assert client.get(f"/api/submissions/{data['id']}").status_code == 404
    assert client.get("/api/submissions").json() == []


def test_login_throttled(client):
    for _ in range(10):
        assert client.post("/api/login", json={"email": "no@example.com", "password": "invalid"}).status_code == 401
    assert client.post("/api/login", json={"email": "no@example.com", "password": "invalid"}).status_code == 429


def test_invalid_request_shapes(client):
    assert client.post("/api/login", json=[]).status_code == 400
    assert client.post("/api/login", content="not-json", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/login", content="{}").status_code == 415
    assert client.post("/api/login", content="{}", headers={"content-length": str(60 * 1024 * 1024)}).status_code == 413
