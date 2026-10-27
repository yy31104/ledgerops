# Frozen PR01 format-1 compatibility bundle

`90000000-0000-4000-8000-000000000001/` was written using the unchanged PR01 exporter
from commit `a067785d2b5addabf98a6121ea87b0f4b14ce4b9` before PR02 exporter changes.
Its rows come from the hand-authored PR01 verifier fixture (opening 100, purchase 80,
customer 20, M1 80, idle M2 0). Timestamps/server-version representation were normalized
to the existing PR01 wire contract. No PostgreSQL capture claim is made for this fixture.

The manifest records dirty=true because this is synthetic fixture generation, not
an assertion of a clean database export. Keep both files frozen. New loaders and
verifiers must accept these exact format-1 bytes after schema 003 is introduced;
generating a replacement with the new exporter would not test that compatibility.
