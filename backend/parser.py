"""Conservative, column-aware extraction for text-based deposit statements.

Bank branding alone does not establish a supported layout. A transaction table
must expose date, debit, credit and balance columns; otherwise review cannot
establish completeness and the whole file is rejected.
"""
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pdfplumber

BANKS = {"BPI", "BDO", "UnionBank"}
SCHEMA = ["bank", "account_number", "source_file", "transaction_date", "description",
          "debit", "credit", "balance", "currency"]
DATE = re.compile(r"^(?:\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?|\d{4}-\d{2}-\d{2}|\d{1,2}[-/]?[A-Za-z]{3,9}(?:[-/]?\d{2,4})?|[A-Za-z]{3,9}[-/]?\d{1,2}(?:[-/]?\d{2,4})?)$")


class StatementError(ValueError):
    pass


def normalize_date(value, year=None, order="MDY"):
    value = value.strip()
    if order not in {"MDY", "DMY"}:
        raise StatementError("Choose MDY or DMY for numeric dates.")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        formats = ["%Y-%m-%d"]
    else:
        value = re.sub(r"[./\s]+", "-", value)
        numeric = "%m-%d" if order == "MDY" else "%d-%m"
        formats = [numeric + "-%Y", numeric + "-%y", "%d-%b-%Y", "%d-%b-%y",
                   "%b-%d-%Y", "%b-%d-%y", "%d%b%Y", "%d%b%y", "%b%d%Y",
                   "%b%d%y", "%d-%B-%Y", "%B-%d-%Y"]
    for fmt in formats:
        try:
            result = datetime.strptime(value, fmt).date()
            if not 1900 <= result.year <= 2100:
                raise StatementError("Date is outside the supported range.")
            return result.isoformat()
        except ValueError:
            pass
    if year:
        for fmt in [numeric if 'numeric' in locals() else "%m-%d", "%d-%b", "%b-%d", "%d%b", "%b%d", "%d-%B", "%B-%d"]:
            try:
                return datetime.strptime(f"{value}-{year}", fmt + "-%Y").date().isoformat()
            except ValueError:
                pass
    raise StatementError(f"Cannot resolve date '{value}'. Supply the transaction year for dates without a year.")


def money(value, *, signed=False):
    value = value.strip().replace(",", "").replace("₱", "")
    value = re.sub(r"\b(?:PHP|PHP\.|P)\s*", "", value, flags=re.I).strip()
    if value in {"", "-", "—"}:
        return "" if signed else "0.00"
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]
    if not re.fullmatch(r"-?\d+(?:\.\d{1,2})?", value):
        raise StatementError(f"Unrecognized monetary value '{value}'.")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise StatementError("Invalid amount.") from exc
    if not signed and amount < 0:
        raise StatementError("Negative debit/credit requires manual reconciliation.")
    if abs(amount) > Decimal("999999999999.99"):
        raise StatementError("Amount exceeds the supported range.")
    return str(amount.quantize(Decimal("0.01")))


def normalize_record(record):
    if set(record) != set(SCHEMA):
        raise StatementError("Transaction fields do not match the CSV schema.")
    row = {key: str(record[key]).strip() for key in SCHEMA}
    if row["bank"] not in BANKS:
        raise StatementError("Unsupported bank.")
    if not re.fullmatch(r"[0-9Xx* -]{4,40}", row["account_number"]):
        raise StatementError("Account number must contain 4–40 digits, masking characters, spaces or hyphens.")
    if not row["description"] or len(row["description"]) > 2000:
        raise StatementError("Each transaction needs a description of at most 2,000 characters.")
    if row["currency"] != "PHP":
        raise StatementError("This version supports PHP deposit statements only.")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["transaction_date"]):
        raise StatementError("Use YYYY-MM-DD for reviewed transaction dates.")
    row["transaction_date"] = normalize_date(row["transaction_date"])
    row["debit"] = money(row["debit"])
    row["credit"] = money(row["credit"])
    row["balance"] = money(row["balance"], signed=True)
    if Decimal(row["debit"]) and Decimal(row["credit"]):
        raise StatementError("A transaction cannot have both a debit and a credit.")
    return row


def lines(words):
    groups = []
    for word in sorted(words, key=lambda w: (round(w["top"] / 3), w["x0"])):
        if groups and abs(groups[-1][0]["top"] - word["top"]) < 4:
            groups[-1].append(word)
        else:
            groups.append([word])
    return [sorted(group, key=lambda w: w["x0"]) for group in groups]


def header_columns(group):
    aliases = {"date": {"date"}, "description": {"description", "particulars", "details", "narration"},
               "debit": {"debit", "debits", "withdrawal", "withdrawals"},
               "credit": {"credit", "credits", "deposit", "deposits"}, "balance": {"balance"}}
    found = {}
    for word in group:
        token = re.sub(r"[^a-z]", "", word["text"].lower())
        for field, names in aliases.items():
            if token in names and field not in found:
                found[field] = (word["x0"], word["x1"])
    if set(found) == set(aliases) and len(set(found.values())) == 5:
        return sorted([(field, *position) for field, position in found.items()], key=lambda item: item[1])
    return None


def extract_statement(data, filename, bank, account="", year=None, order="MDY"):
    if bank not in BANKS:
        raise StatementError("Choose BPI, BDO or UnionBank.")
    if year is not None and not 1900 <= int(year) <= 2100:
        raise StatementError("Transaction year must be between 1900 and 2100.")
    if order not in {"MDY", "DMY"}:
        raise StatementError("Choose MDY or DMY for numeric dates.")
    if not data.startswith(b"%PDF-"):
        raise StatementError("The uploaded file is not a PDF.")
    rows = []
    missing_year = False
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if not 1 <= len(pdf.pages) <= 50:
                raise StatementError("Statements must contain between 1 and 50 pages.")
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            brand = {"BPI": r"\bBPI\b|Bank of the Philippine Islands", "BDO": r"\bBDO\b|Banco de Oro",
                     "UnionBank": r"Union\s*Bank|Unionbank"}[bank]
            if not re.search(brand, text, re.I):
                raise StatementError(f"The PDF does not identify itself as {bank}. Check the selected bank.")
            detected = re.findall(r"Account\s*(?:Number|No\.?|#)\s*[:\-]?\s*([0-9Xx*][0-9Xx* -]{3,39})", text, re.I)
            detected = {re.sub(r"[ -]", "", a.strip()) for a in detected}
            if len(detected) > 1:
                raise StatementError("Multiple accounts detected. Upload one account per PDF.")
            if not account and len(detected) == 1:
                account = detected.pop()
            if not account:
                raise StatementError("Account number could not be read. Enter it for this PDF.")
            if detected and re.sub(r"[ -]", "", account) not in detected:
                raise StatementError("Entered account number does not match the PDF. Use the exact number or mask shown.")
            for page_number, page in enumerate(pdf.pages, 1):
                columns = None
                previous_balance = None
                for group in lines(page.extract_words()):
                    header = header_columns(group)
                    if header:
                        columns = header
                        continue
                    line = " ".join(w["text"] for w in group)
                    if not columns:
                        # Never omit date-looking transaction rows on an unsupported page.
                        if (DATE.fullmatch(group[0]["text"]) or re.match(r"(?:\d{1,4}[/.-]\d|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", group[0]["text"], re.I)) and re.search(r"\d[,.]\d{2}\b", line):
                            raise StatementError(f"Page {page_number}: transaction columns could not be identified.")
                        continue
                    cells = {key: [] for key, _, _ in columns}
                    for word in group:
                        # Table values can be right aligned; assign by header start.
                        key = columns[0][0]
                        for field, start, _ in columns:
                            if word["x0"] >= start - 8:
                                key = field
                        numeric_columns = [(field, start, end) for field, start, end in columns if field in {"debit", "credit", "balance"}]
                        if re.fullmatch(r"(?:PHP|₱|P)?-?[\d,]+\.\d{2}|\([\d,]+\.\d{2}\)|[-—]", word["text"], re.I):
                            closest = min(numeric_columns, key=lambda col: abs(word["x1"] - col[2]))
                            if abs(word["x1"] - closest[2]) <= 65 and word["x0"] >= numeric_columns[0][1] - 65:
                                key = closest[0]
                        cells[key].append(word["text"])
                    cells = {key: " ".join(value) for key, value in cells.items()}
                    if re.match(r"^(?:Total\b|(?:Opening|Beginning|Closing|Ending)\s+Balance\b|Balance\s+(?:Brought|Carried)\s+Forward\b)", line, re.I):
                        if re.match(r"^(?:Opening|Beginning)\s+Balance\b|^Balance\s+Brought\s+Forward\b", line, re.I) and cells["balance"]:
                            previous_balance = Decimal(money(cells["balance"], signed=True))
                        continue
                    date = cells["date"].strip()
                    if not date:
                        if any(re.search(r"\d", cells[k]) for k in ["debit", "credit", "balance"]):
                            raise StatementError(f"Page {page_number}: an amount row has no readable date. This layout requires manual reconciliation.")
                        if rows and cells["description"] and not any(cells[k] for k in ["debit", "credit", "balance"]):
                            rows[-1]["description"] += " " + cells["description"]
                        continue
                    if not DATE.fullmatch(date.replace(" ", "")):
                        if re.match(r"\d{1,4}[/.-]\d", date) or (re.match(r"\d|[A-Za-z]{3}", date) and any(re.search(r"\d[,.]\d{2}", cells[k]) for k in ["debit", "credit", "balance"])):
                            raise StatementError(f"Page {page_number}: unreadable transaction date '{date}'.")
                        continue
                    try:
                        normalize_date(date, order=order)
                    except StatementError:
                        missing_year = True
                    row = normalize_record({"bank": bank, "account_number": account, "source_file": filename,
                        "transaction_date": normalize_date(date, year, order), "description": cells["description"],
                        "debit": cells["debit"], "credit": cells["credit"], "balance": cells["balance"], "currency": "PHP"})
                    if not Decimal(row["debit"]) and not Decimal(row["credit"]):
                        raise StatementError(f"Page {page_number}: a dated row has no debit or credit. Check column alignment or remove balance-only rows from the source PDF.")
                    if previous_balance is not None and row["balance"]:
                        expected = previous_balance - Decimal(row["debit"]) + Decimal(row["credit"])
                        if expected != Decimal(row["balance"]):
                            raise StatementError(f"Page {page_number}: running balance does not reconcile. This layout requires manual review outside the converter.")
                    previous_balance = Decimal(row["balance"]) if row["balance"] else None
                    rows.append(row)
    except StatementError:
        raise
    except Exception as exc:
        raise StatementError("PDF could not be read. Use an unlocked, text-based statement; scanned PDFs need OCR first.") from exc
    if not rows:
        raise StatementError("No supported transaction table found. Scanned PDFs, credit-card statements and unfamiliar layouts are not supported yet.")
    if missing_year and {"01", "12"}.issubset({row["transaction_date"][5:7] for row in rows}):
        raise StatementError("This PDF includes January and December with dates missing years. Use a statement containing full dates to avoid assigning the wrong year.")
    return rows
