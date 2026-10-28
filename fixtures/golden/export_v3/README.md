# Frozen resume-review format-3 compatibility bundle

`90000000-0000-4000-8000-000000000003/` was written using the unchanged exporter
from `3d4901f8629007e0ce0a57f15f6a0102f21a1b2c` before reversal changes.
Its hand-authored rows describe opening 100, purchase 80, customer 20, merchant 80,
a complete repair audit and a closed freeze retaining its healthy resume report.
Timestamps/server version were normalized to the format-3 wire contract. This is
a synthetic compatibility fixture, not a PostgreSQL capture; dirty=true is explicit.

Keep both JSON members frozen. New readers must accept these exact bytes after
schema 005 adds reversal relations, without regenerating them using the new exporter.
