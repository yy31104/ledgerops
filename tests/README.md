# Tests

No business tests exist at initialization. Do not treat a package import or lint check
as proof of domain correctness. `make test` runs pytest and currently exits with code 5
(no tests collected); no placeholder passing tests or unconditional skips are added.

The first task contract under `docs/tasks/` defines the meaningful tests to add.
PostgreSQL claims must eventually be verified with a real server and runtime role.
Expected results must not import production calculation/posting helpers.
