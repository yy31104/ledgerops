UV ?= uv
UV_CACHE_DIR ?= $(CURDIR)/.cache/uv
export UV_CACHE_DIR
CHECK_DIRS := src tests reference $(wildcard provider_sim) $(wildcard ops)

.PHONY: bootstrap check test test-unit test-integration test-provider build demo benchmark service

bootstrap:
	$(UV) sync --locked

check:
	$(UV) run --locked ruff check $(CHECK_DIRS)
	$(UV) run --locked ruff format --check $(CHECK_DIRS)
	$(UV) run --locked --no-editable --reinstall-package ledgerops python -c "import ledgerops.service, ledgerops.http.application, ledgerops.auth_cli, ledgerops.authentication, ledgerops.cli, ledgerops.export, ledgerops.verification, ledgerops.db.maintenance, ledgerops.provider_protocol, ledgerops.provider_client, ledgerops.worker, ledgerops.reconciliation, ledgerops.reconciliation.publication, ledgerops.reconciliation.runner, ledgerops.recon_cli; from ledgerops.db.admission import submit_reversal, submit_payout, get_payout_state; from ledgerops.db.payout_dispatch import dispatch_once; from ledgerops.db.payout_resolution import receive_observation; from ledgerops.domain import ReversalCommand, PayoutCommand; from importlib.resources import files; assert all(files('ledgerops.db').joinpath('sql', name).is_file() for name in ('001_foundation.sql', '002_export_reader.sql', '003_projection_maintenance.sql', '004_resume_review.sql', '005_full_reversal.sql', '006_payout_admission.sql', '007_payout_resolution.sql', '008_reconciliation_publication.sql', '009_authentication.sql', '010_operator_actions.sql', '011_active_reservation_index.sql')); print('ledgerops: fresh installed modules and SQL resources OK')"

test:
	$(UV) run --locked python -m pytest

test-unit:
	$(UV) run --locked python -m pytest tests/unit tests/reference

test-integration:
	$(UV) run --locked python -m pytest tests/integration

test-provider:
	$(UV) run --locked python -m pytest tests/provider tests/unit/test_provider_store.py tests/unit/test_provider_http_validation.py

build:
	$(UV) build

demo:
	@$(UV) run --quiet --locked python -m pytest -qq -s --no-header --tb=short -p tests.demo_output tests/demo_story.py
	@printf '%s\n' 'Demo complete. Temporary PostgreSQL and exported files have been removed.'

benchmark:
	$(UV) run --locked python -m pytest -q -s --no-header --tb=short tests/benchmark_story.py

service:
	$(UV) run --locked python -m ledgerops.service
