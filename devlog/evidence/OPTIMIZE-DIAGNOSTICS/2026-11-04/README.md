# Linux diagnostics and startup readiness

Sixteen focused tests passed, including actual secret-bearing subprocess output, bounded private retention, startupCA predicates and constructor/disposal failure preservation. A real missing-container Docker call demonstrates actualstderr retention; only metadata is copied here. Raw bytes stay outside the checkout in a0700 systemtemporary directory with0600 files. They are intentionally not a publishable artifact.

The old startupfailure remains unexplained; this change makes future command failures diagnosable without publishing credentials and observes readiness rather than assuming a fixedsleep. Root integrates full regression and clean-image nine-group acceptance separately.
