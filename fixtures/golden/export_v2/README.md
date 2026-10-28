# Frozen PR02 format-2 compatibility bundle

`90000000-0000-4000-8000-000000000002/` was written using the unchanged PR02 exporter
from commit `d825d41196cdc169ef262eeaa17f9fa47fc68ebe` before resume-review changes.
Rows come from the hand-authored PR02 verifier fixture: opening 100, purchase 80,
customer 20, M1 80, idle M2 0, an open maintenance freeze and a four-account repair
audit advancing projection epoch from 1 to 2. Timestamps/server version were normalized
to the existing PR02 wire contract. This does not claim a PostgreSQL capture.

The manifest records dirty=true for synthetic fixture generation. Preserve these
exact format-2 files. New loaders/verifiers must accept them after schema 004 adds
resume reports; regenerating them with new code would not test compatibility.
