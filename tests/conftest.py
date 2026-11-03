import io
import json

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas

from backend import main


def statement_pdf(bank="BPI", account="0012345678", rows=None, headers=None, right_aligned=False):
    stream = io.BytesIO()
    canvas = Canvas(stream, pagesize=(700, 800))
    canvas.setFont("Helvetica", 10)
    canvas.drawString(40, 760, f"{bank} Statement of Account")
    canvas.drawString(40, 740, f"Account Number: {account}")
    positions = [40, 130, 380, 480, 580]
    for x, text in zip(positions, headers or ["Date", "Description", "Debit", "Credit", "Balance"]):
        canvas.drawString(x, 700, text)
    for index, row in enumerate(rows or [["11/01/2026", "Opening deposit", "", "1,000.00", "1,000.00"], ["11/02/2026", "Coffee shop", "150.25", "", "849.75"]]):
        for x, text in zip(positions, row):
            if right_aligned and x >= 380:
                canvas.drawRightString(x + 35, 680 - 20 * index, text)
            else:
                canvas.drawString(x, 680 - 20 * index, text)
    canvas.save()
    return stream.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(main, "SECURE_COOKIES", False)
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-password-12345")
    with TestClient(main.app) as client:
        yield client


def login(client, email="user@example.com", password="user-password-12345", register=True):
    if register:
        response = client.post("/api/register", json={"name": "Test User", "email": email, "password": password, "role": "admin"})
        assert response.status_code == 200
    result = client.post("/api/login", json={"email": email, "password": password})
    assert result.status_code == 200, result.text
    client.headers["x-csrf-token"] = result.json()["csrf"]
    return result.json()["user"]


def upload(client, banks=("BPI", "BDO", "UnionBank")):
    metadata, files = [], []
    for index, bank in enumerate(banks):
        metadata.append({"bank": bank, "year": 2026, "order": "MDY"})
        files.append(("files", (f"{bank}.pdf", statement_pdf(bank, f"000{index}123456"), "application/pdf")))
    return client.post("/api/submissions", files=files, data={"title": "November statements", "metadata": json.dumps(metadata)})
