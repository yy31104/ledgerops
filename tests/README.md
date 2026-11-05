# Tests

Run `make bootstrap`, then `make test` for the complete suite. Docker is required:
the integration fixtures start a disposable PostgreSQL container and fail clearly
when it is unavailable. Each test owns its database and connections; session cleanup
removes the owned container.

`make test-unit` runs the unit tests and independent journal/command model.
`make test-integration` runs the PostgreSQL cases. `make test-provider` exercises
the separate persistent provider and its HTTP protocol.

Expected financial results stay independent of production posting and calculation
helpers. Integration tests cover commit-time constraints, ordered locks, concurrent
requests, unknown results and recovery. Current operating instructions are in `docs/`.

`tests/integration/test_model_based_purchases.py` is a Hypothesis state machine. It
generates random sequences of purchases, reversals, exact retries, reused keys and
operation IDs, wrong actors and customer funding, sends each step to both
`reference/command_model.py` and the production admission functions, and checks after
every step that outcomes, balances and journal counts agree. Set
`LEDGEROPS_MODEL_EXAMPLES` and `LEDGEROPS_MODEL_STEPS` to search longer; under `CI` the
search is derandomized so a run is reproducible.
