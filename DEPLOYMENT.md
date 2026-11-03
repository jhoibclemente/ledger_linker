# Deploy Ledger Linker on free Render hosting

The application serves both `/app/` and `/api/` on one HTTPS origin. The
`render.yaml` blueprint selects Render's **free web-service tier** and does not
create a paid service or persistent disk. Supabase's free PostgreSQL database
stores accounts, sessions, settings and submission records; a private Supabase
Storage bucket stores original PDFs. These services remain free only within
currently published free-tier limits.

GitHub Pages remains a static landing page and UI. It cannot authenticate users,
store submissions or run PDF conversion. The Pages login screen reports when
account services are unavailable instead of storing private data in the browser.

[Deploy on Render](https://render.com/deploy?repo=https://github.com/jhoibclemente/ledger_linker)

## Create the Supabase project

1. Create a **Free** project in your Supabase account. Choose a strong database
   password and store it privately. No paid Supabase plan is required by this app.
2. In **Storage**, create a bucket named `ledger-statements`. Keep **Public bucket
   disabled**. Do not add anonymous upload/download policies or publish this
   bucket. The backend accesses objects with its server-only service-role key;
   every browser request must pass application authentication and ownership checks.
3. From the project's **Connect** panel, copy the PostgreSQL connection URI. Prefer
   the **session pooler** endpoint when a direct endpoint is IPv6-only, because
   Render free services need a reachable connection. Use the URI intended for
   PostgreSQL clients such as psycopg; add `sslmode=require` to require TLS.
   Percent-encode special characters in the database password when constructing
   a URI. Do not put the URI in source code, browser configuration, or chat.
4. In the project settings, obtain the project URL and **service_role** API key.
   The anonymous/publishable key is not sufficient. The service-role key can
   bypass Supabase access policies and must be kept only in the backend's private
   environment. Never place it in `app/`, GitHub Pages, or a public Git repository.

## Deploy the Render service

1. Open the deployment link while signed into Render, connect this repository,
   and deploy its `main` branch using `render.yaml`. Confirm the web-service plan
   says **Free** and that no disk or paid database is being created.
2. Enter these values in Render's private environment settings:

   | Variable | Value |
   | --- | --- |
   | `STORAGE_BACKEND` | `supabase` (already provided by the blueprint) |
   | `DATABASE_URL` | Supabase PostgreSQL URI with `sslmode=require` |
   | `SUPABASE_URL` | Your project's HTTPS URL |
   | `SUPABASE_SERVICE_ROLE_KEY` | Server-only Supabase service-role key |
   | `SUPABASE_BUCKET` | `ledger-statements` (already provided by the blueprint) |
   | `ADMIN_EMAIL` | Email for your initial administrator |
   | `ADMIN_PASSWORD` | A unique password of 12–128 characters |
   | `SECURE_COOKIES` | `true` (already provided by the blueprint) |

   Enter credentials in Render, never in this repository or chat. There are no
   default passwords. The app initializes its database tables in the
   `ledger_private` schema on startup, revokes public schema access and enables
   row-level security with no browser-access policies. Keep this schema outside
   Supabase's exposed Data API schemas. The PostgreSQL account needs permission
   to create and own these application tables; the session-pooler connection for
   the project's database owner supports this setup.
3. Wait for `/api/health` to become healthy. Open the HTTPS service URL Render
   assigns, then `/app/`. Choose **Administrator**, sign in and review settings.
4. Create a regular user with **Create account**. Upload a small supported
   statement, review the extracted transactions, confirm the batch and download
   the CSV. Restart the service and verify that the same submission and its
   original PDF remain accessible.
5. Remove `ADMIN_PASSWORD` from Render's environment after creating the initial
   administrator. The existing account and password hash remain in PostgreSQL
   across restarts. Changing this bootstrap variable does not reset an existing
   administrator's password.
6. Share the public Render URL. Point the GitHub Pages landing page's two `/app/`
   links to that URL after it is known. The static Pages interface intentionally
   does not send credentials or statements to an unconfigured external server.

Creating the Supabase project and Render service requires access to your
accounts. Committing the blueprint does not create or deploy either service.
Subsequent pushes deploy according to the Render service's auto-deploy setting.

## Free-tier behavior and operations

- Render free web services sleep after inactivity. The first request after sleep
  can take longer while the service starts. Free runtime quotas, restarts and
  suspension can interrupt processing. This setup does not promise continuous
  availability.
- Render's local filesystem is temporary. Production accounts, sessions,
  settings, records and original PDFs must live in Supabase; do not use the
  development SQLite mode for this deployment. A restart should not erase stored
  submissions, and no paid Render disk is needed.
- `STORAGE_BACKEND=supabase` requires all database and private-storage credentials.
  Incomplete cloud configuration fails startup instead of silently switching to
  temporary SQLite storage.
- Supabase's free database, object storage and bandwidth have separate quotas,
  and inactive free projects may be paused. Review the current
  [Render free-service limits](https://render.com/docs/free) and
  [Supabase pricing limits](https://supabase.com/pricing). Monitor usage and remove
  unneeded submissions before reaching those limits. Neither provider's free tier
  is an unlimited storage or backup service.
- Keep the bucket private. The browser receives original PDFs only through the
  authenticated backend; do not return public object URLs or expose service-role
  credentials. Use a dedicated Supabase project for this app and restrict access
  to its dashboard and deployment environment.
- The browser never connects directly to PostgreSQL or to application tables
  through Supabase's Data API. Keep `ledger_private` private. Anonymous and
  authenticated Supabase roles must not gain access to password hashes, sessions,
  submissions or settings. The server's PostgreSQL owner connection performs
  application operations after checking the logged-in Ledger Linker account.
- Run a single application instance / single Uvicorn worker as configured. Keep
  HTTPS-only, HttpOnly, SameSite=Strict cookies enabled. Sessions expire after
  eight hours. Both UI and API must use the same browser origin.
- Render terminates HTTPS at its proxy. If adding another proxy or custom domain,
  set `APP_ORIGIN=https://your-domain.example` to the canonical browser origin.
- Establish private database and object-storage backups separately and test
  restoring both. Do not assume paid automatic backup features exist on a free
  plan. Treat every backup as private financial information.
- Users may keep 100 submissions each. A batch is at most 50 MB / 10,000 rows,
  with 1–10 PDFs, 1–20 MB per PDF as configured by an admin, and 50 pages per PDF.
  Supabase's project and bucket limits can be lower than these application limits;
  reduce the app limits as needed. Deleting a submission removes its stored
  records and original objects; backup retention is separate.
- Parsing is limited to one process, 40 seconds wall time and 256 MB address
  space per PDF. Authentication, registration and conversion requests are rate
  limited. Statement contents and passwords are not written to application logs.
- Uploads record a private cleanup intent before writing remote objects. If an
  upload or database commit fails, the app removes unreferenced objects when it
  can confirm that no submission committed. Unfinished intents older than one
  hour are retried on startup and on later uploads. Failed object deletion keeps
  submission metadata so deletion can be retried. Database and object storage
  are separate services, so an interrupted deletion can temporarily leave a
  submission whose originals have already been removed; retry deletion to finish.
- Password changes revoke all sessions. Free Render services do not provide the
  paid service shell. For account recovery or provisioning an additional admin,
  use an authorized local Python environment with the same remote database and
  storage variables set privately:

  ```sh
  python -m backend.admin reset-password user@example.com
  python -m backend.admin create-admin another-admin@example.com
  ```

  These commands prompt for passwords without echoing them or putting them in
  command arguments. `create-admin` only creates a new account; public
  registration always creates a regular user. The bootstrap administrator is
  created only when no admin exists.

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

SQLite is available for local development without creating cloud accounts:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
export SECURE_COOKIES=false
export DATA_DIR=./data
# Leave remote database/storage variables unset for isolated local development.
# Leave STORAGE_BACKEND unset (or set it to sqlite) for the local SQLite backend.
# Set ADMIN_EMAIL and ADMIN_PASSWORD privately for initial provisioning.
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/app/`. The backend serves the UI without a JavaScript
build. Use a temporary data directory for development. Do not commit private
PDFs, databases, credentials or screenshots containing real statements.

To exercise the cloud backend manually, configure a separate test Supabase
project using the same environment variables as production. Automated remote
integration tests use `TEST_POSTGRES_URL` pointing to a disposable local PostgreSQL
database and mock the object-storage HTTP boundary. They delete and recreate
`ledger_private`; **never set this test variable to a production project**.
CI starts a temporary PostgreSQL service for those tests.

```sh
python -m pytest -q
node --check app/app.js
python -m playwright install chromium
python -m tests.browser_smoke
```
