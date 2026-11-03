# Deploy the Ledger Linker application

The shared application runs on **Render**, serving both `/app/` and `/api/`
on the same HTTPS origin. GitHub Pages remains a static landing page and UI;
it cannot authenticate users, store submissions, or run the converter. The
static login screen clearly reports when account services are unavailable.

[Deploy on Render](https://render.com/deploy?repo=https://github.com/jhoibclemente/ledger_linker)

## First deployment

1. Open the deployment link while signed into your Render account, connect this
   repository and deploy its `main` branch using `render.yaml`.
2. Review the **paid Starter web service** and **1 GB persistent disk** in Singapore.
   The disk is required: an ephemeral/free instance would lose accounts and files.
3. Set `ADMIN_EMAIL` to your administrator email and `ADMIN_PASSWORD` to a unique
   password of 12–128 characters. Enter credentials in Render's private environment
   settings, never in this repository or chat. There are no default credentials.
4. Wait for `/api/health` to become healthy. Open the HTTPS service URL Render
   assigns, then `/app/`. Choose **Administrator**, sign in and review settings.
5. Create a regular user via **Create account**. Upload statements, review the
   extracted transactions, confirm the batch and download the aggregated CSV.
6. Remove `ADMIN_PASSWORD` from Render's environment after the initial admin is
   created. Restarting preserves the admin and password hash on the disk.
7. Share the Render application URL. Point the GitHub Pages landing page's two
   `/app/` links to this URL after it is known; the Pages copy of the interface
   intentionally does not send private data to an unconfigured external server.

Render account access is required to create the service. Merely committing the
blueprint does not deploy a backend. Subsequent pushes are deployed according to
the auto-deploy setting on the Render service.

## Storage and operations

- Run a **single instance / single Uvicorn worker** with `/var/data` mounted.
  SQLite, user accounts, sessions, settings, parsed records and original PDFs
  persist on this disk. Do not scale multiple instances against this database.
- HTTPS-only, HttpOnly, SameSite=Strict cookies expire after eight hours. Keep
  `SECURE_COOKIES=true` in production. Both UI and API must use the same origin.
- The app trusts proxy scheme headers for Render HTTPS termination. It must be
  accessed through Render's proxy. If adding another proxy/domain, set
  `APP_ORIGIN=https://your-domain.example` to the canonical browser origin.
- Enable disk snapshots/backups in Render and test restoration. For an independent
  consistent SQLite backup, use Python's `sqlite3.Connection.backup` from the
  service shell to a protected backup destination. Copying only a live `.sqlite3`
  file can omit WAL data. Treat every backup as private financial information.
- Users may keep 100 submissions each. A batch is at most 50 MB / 10,000 rows,
  with 1–10 PDFs, 1–20 MB per PDF as configured by an admin, and 50 pages per PDF.
  Storage rejects uploads when less than 100 MB would remain. Monitor disk usage
  and expand the disk or delete unneeded submissions. Deleting a submission
  removes its originals and records from the live database; SQLite may reuse
  freed pages without shrinking the disk file. Backup retention is separate.
- Parsing is limited to one process, 40 seconds wall time and 256 MB address
  space per PDF. Authentication, registration and conversion requests are rate
  limited. No statement contents or passwords are written to application logs.
- Password changes revoke all sessions. Password recovery is currently handled
  by the server administrator in the Render shell:

  ```sh
  python -m backend.admin reset-password user@example.com
  python -m backend.admin create-admin another-admin@example.com
  ```

  Commands prompt for passwords without echoing them or putting them in argv.
  `create-admin` only creates a new account; public registration always creates
  a regular user. The initial admin is created only when no admin exists.

## Supported statements and review

This first version handles **text-based PHP deposit statements** branded BPI,
BDO or UnionBank with a recognized single-line column header. It maps Date;
Description/Particulars/Details/Narration; Debit/Withdrawal(s);
Credit/Deposit(s); and Balance. It is **not yet verified against real statement
samples from these banks**. Synthetic PDFs cover the layouts in automated tests.
Bank template revisions may require adapting column detection.

Scanned images, locked PDFs, credit-card statements, combined debit/credit
columns, unknown layouts and multiple accounts in one PDF are rejected. OCR is
not implemented. Upload one account per PDF. Failed parsing rejects the whole
batch rather than saving a partial submission. Running balances are checked
within each page; opening balances and page boundaries still require review.

Choose the numeric date order explicitly for each PDF. Dates with full years
preserve their years; dates without a year use the year the user supplies.
January/December ambiguity with missing years is rejected. A statement spanning
two years must include complete transaction dates. Review the extraction against
the original PDFs before confirming; rows cannot be deleted during review.
Wrapped descriptions are supported only when they do not include amount cells.

CSV columns are:

```text
bank,account_number,source_file,transaction_date,description,debit,credit,balance,currency
```

All sources in one batch produce one CSV sorted by bank, account, ISO date and
source filename. Accounts remain separate row identifiers, including leading
zeros and masks. Amounts use decimal arithmetic with two fractional digits and
separate debit/credit columns; negative balances are supported. Currency is PHP.
Overlapping statements are not deduplicated, since identical legitimate
transactions must not be silently removed. Treat duplicate-looking entries as
part of the required review. Untrusted CSV text is escaped against spreadsheet
formulas. In Excel, use **Data → From Text/CSV** and set `account_number` to
**Text** to prevent Excel stripping leading zeros. CSV itself has no type system.

Users can access only their submissions, CSVs and original PDFs. Admins can view,
review, export and delete every submission, and change registration/upload limits.
The server enforces those rules independently of the interface.

## Local development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
export SECURE_COOKIES=false
export DATA_DIR=./data
# Set ADMIN_EMAIL and ADMIN_PASSWORD privately for initial provisioning.
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/app/`. The backend serves the UI without a JS build.
Use a temporary data directory for development. Do not commit private PDFs,
databases, credentials or screenshots containing real statements.

```sh
python -m pytest -q
node --check app/app.js
python -m playwright install chromium
python -m tests.browser_smoke
```
