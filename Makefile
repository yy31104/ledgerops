UV ?= uv
UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_CACHE_DIR

.PHONY: bootstrap check test test-unit test-integration build

bootstrap:
	$(UV) sync --locked

check:
	$(UV) run --locked ruff check src tests reference
	$(UV) run --locked ruff format --check src tests reference
	$(UV) run --locked --no-editable python -c "import ledgerops; from importlib.resources import files; assert files('ledgerops.db').joinpath('sql/001_foundation.sql').is_file(); print('ledgerops: installed package and SQL resource OK')"

test:
	$(UV) run --locked python -m pytest

test-unit:
	$(UV) run --locked python -m pytest tests/unit tests/reference

test-integration:
	$(UV) run --locked python -m pytest tests/integration

build:
	$(UV) build
