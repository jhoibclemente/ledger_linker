"""Drop privileges before serving; only local development uses a data directory."""
import os
from pathlib import Path


def main():
    data = Path(os.environ.get("DATA_DIR", "/var/data"))
    if os.getuid() == 0:
        if os.environ.get("STORAGE_BACKEND", "sqlite") == "sqlite":
            data.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(data, 10001, 10001)
            os.chmod(data, 0o700)
            for name in ["ledger.sqlite3", "ledger.sqlite3-wal", "ledger.sqlite3-shm"]:
                path = data / name
                if path.exists() and not path.is_symlink():
                    os.chown(path, 10001, 10001)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.umask(0o077)
    os.execvp("uvicorn", ["uvicorn", "backend.main:app", "--host", "0.0.0.0",
        "--port", os.environ.get("PORT", "10000"), "--workers", "1", "--proxy-headers",
        "--forwarded-allow-ips", "*", "--limit-concurrency", "20", "--timeout-keep-alive", "5"])


if __name__ == "__main__":
    main()
