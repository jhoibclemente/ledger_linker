"""One bounded process per PDF; the parent also enforces a wall-clock timeout."""
import base64
import json
import sys

from backend.parser import StatementError, extract_statement


def main():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        data = json.load(sys.stdin)
        records = extract_statement(base64.b64decode(data.pop("data")), **data)
        json.dump({"records": records}, sys.stdout)
    except StatementError as exc:
        json.dump({"error": str(exc)}, sys.stdout)
    except Exception:
        json.dump({"error": "This PDF exceeds processing limits or could not be read. Use a smaller, unlocked text-based PDF."}, sys.stdout)


if __name__ == "__main__":
    main()
