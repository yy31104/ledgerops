# v1.2 development label changes

## v1-007, label version 2

Question: "A payout was refused for lack of funds. If the merchant has funds now,
will resending the same request succeed?"

| | Version 1 (v1-draft, v4-dev, v11-dev) | Version 2 (this file) |
|---|---|---|
| Fact 1 | No: replay returns the original retained refusal | unchanged |
| Fact 2 | A new attempt needs a new operation ID and key | A new attempt needs a new operation ID |

Reason: the indexed sources state that insufficient funds produce a retained 422,
that adding funds does not change an earlier retained refusal, that replay returns
the original result and that new operations check available funds
(`docs/PAYOUTS.md#amounts-and-admission-receipts`). No indexed passage says that
the new attempt also needs a new Idempotency-Key. `docs/HTTP-SERVICE.md#api` only
says that a retry after a disconnect must keep the original operation ID and key.
The missing part is recorded in the row's `source_gap` field.

The question, route and relevant sections are unchanged. Version 1 stays in the
earlier files and in the v1.1 human-review worksheet, which was exported before
this change. A reviewer grading v1-007 should not mark an answer incomplete only
because it omits the new key.

No other label changes. The 85 v1.1 holdout rows are copied with `split` set to
`dev`; their wording, facts and sections are unchanged.
