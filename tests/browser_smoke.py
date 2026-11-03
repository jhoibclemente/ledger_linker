"""Run with `python -m tests.browser_smoke`; uses an isolated test database."""
import csv
import io
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from tests.conftest import statement_pdf


def main():
    artifacts = Path("artifacts")
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        password = secrets.token_urlsafe(20)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "DATA_DIR": directory, "SECURE_COOKIES": "false",
               "ADMIN_EMAIL": "admin@example.com", "ADMIN_PASSWORD": password}
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    if httpx.get(base + "/api/health", trust_env=False).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(.1)
            else:
                raise AssertionError("Test server did not start")
            with sync_playwright() as playwright:
                # CI must use the Playwright-managed revision, not Ubuntu's
                # optional Chromium/snap wrapper discovered on PATH.
                executable = os.environ.get("CHROMIUM_PATH") or (None if os.environ.get("CI") else shutil.which("chromium"))
                browser = playwright.chromium.launch(executable_path=executable, args=["--no-sandbox"])
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/app/")
                expect(page.locator("#auth-submit")).to_be_enabled()
                page.screenshot(path=str(artifacts / "login-desktop.png"), full_page=True)
                page.get_by_role("button", name="Create account", exact=True).click()
                page.locator("#name").fill("Jamie Test")
                page.locator("#email").fill("jamie@example.com")
                page.locator("#password").fill(password)
                page.locator("#auth-submit").click()
                expect(page.locator("#auth-title")).to_have_text("Welcome back.")
                expect(page.locator("#auth-submit")).to_contain_text("Sign in")
                page.locator("#password").fill(password)
                page.locator("#auth-submit").click()
                expect(page.locator("#workspace-view")).to_be_visible()
                expect(page.locator("#settings-nav")).to_be_hidden()
                page.locator("#batch-title").fill("November · Three bank accounts")
                files = []
                for index, bank in enumerate(["BPI", "BDO", "UnionBank"]):
                    path = Path(directory) / f"{bank}.pdf"
                    path.write_bytes(statement_pdf(bank, f"000{index}123456"))
                    files.append(str(path))
                page.locator("#pdf-files").set_input_files(files)
                for index, bank in enumerate(["BPI", "BDO", "UnionBank"]):
                    page.locator(f"#file-{index}-bank").select_option(bank)
                page.screenshot(path=str(artifacts / "workspace-desktop.png"), full_page=True)
                page.locator("#convert").click()
                expect(page.locator("#review-view")).to_be_visible(timeout=30000)
                expect(page.locator("#review-rows tr")).to_have_count(6)
                expect(page.locator("#confirm")).to_be_disabled()
                page.locator("#review-check").check()
                page.locator("#confirm").click()
                expect(page.locator("#download-ready")).to_be_visible()
                with page.expect_download() as download_info:
                    page.locator("#download-ready").click()
                download = download_info.value
                rows = list(csv.DictReader(io.StringIO(Path(download.path()).read_text(encoding="utf-8-sig"))))
                assert len(rows) == 6
                assert {row["bank"] for row in rows} == {"BPI", "BDO", "UnionBank"}
                page.screenshot(path=str(artifacts / "review-desktop.png"), full_page=True)
                page.locator('[data-view="history"]').click()
                expect(page.locator(".submission-card")).to_have_count(1)
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('[data-view="upload"]').click()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(artifacts / "workspace-mobile.png"), full_page=True)
                page.locator("#signout").click()
                expect(page.locator("#auth-view")).to_be_visible()
                page.locator("#email").fill("admin@example.com")
                page.locator("#password").fill(password)
                page.locator("#access").select_option("admin")
                page.locator("#auth-submit").click()
                expect(page.locator("#history-title")).to_have_text("All submissions")
                expect(page.locator(".submission-card")).to_contain_text("jamie@example.com")
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.screenshot(path=str(artifacts / "admin-desktop.png"), full_page=True)
                page.locator("#settings-nav").click()
                page.locator("#max-files").fill("5")
                page.locator("#settings-form button").click()
                expect(page.locator("#notice")).to_have_text("Workspace settings saved.")
                page.reload()
                page.locator("#settings-nav").click()
                expect(page.locator("#max-files")).to_have_value("5")
                assert not errors, errors
                browser.close()
                print("Browser flow passed: registration, login, 3-bank PDF upload, review, CSV download, mobile layout, admin access and persisted settings.")
        except Exception:
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a") as report:
                    report.write("## Browser verification failure\n\n```text\n" + traceback.format_exc() + "\n```\n")
            raise
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
