"""Entry point for the isolated performance experiment; no service changes."""

from ledgerops_assistant.benchmark import main

if __name__ == "__main__":
    raise SystemExit(main())
