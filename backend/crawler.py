"""Compatibility entry point for the collection worker."""
from backend.worker import main, worker


if __name__ == "__main__":
    raise SystemExit(main())
