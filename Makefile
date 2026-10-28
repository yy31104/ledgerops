UV ?= uv
UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_CACHE_DIR

.PHONY: bootstrap check test test-unit test-integration build demo

bootstrap:
	$(UV) sync --locked

check:
	$(UV) run --locked ruff check src tests reference
	$(UV) run --locked ruff format --check src tests reference
	$(UV) run --locked --no-editable --reinstall-package ledgerops python -c "import ledgerops.cli, ledgerops.export, ledgerops.verification, ledgerops.db.maintenance; from ledgerops.db.admission import submit_reversal; from ledgerops.domain import ReversalCommand; from importlib.resources import files; assert all(files('ledgerops.db').joinpath('sql', name).is_file() for name in ('001_foundation.sql', '002_export_reader.sql', '003_projection_maintenance.sql', '004_resume_review.sql', '005_full_reversal.sql')); print('ledgerops: fresh installed modules and SQL resources OK')"

test:
	$(UV) run --locked python -m pytest

test-unit:
	$(UV) run --locked python -m pytest tests/unit tests/reference

test-integration:
	$(UV) run --locked python -m pytest tests/integration

build:
	$(UV) build

demo:
	@$(UV) run --locked python -m pytest -q -s --no-header --tb=short tests/demo_story.py
	@printf '%s\n' '演示完成，临时 PostgreSQL 和导出文件已清理。'
