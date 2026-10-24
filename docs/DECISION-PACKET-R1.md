# R1 decision packet 1 — accepted

Date: 2026-10-23. Yi explicitly replied **沿用这组默认** to this four-item
packet in the implementation chat. This accepts the design choices below, not a
claim of personal mastery. Implementation remains local.

| Choice | Accepted default and example | Alternative and cost | Counterexample / review prompt |
|---|---|---|---|
| Accounting perspective | Clearing is a platform asset; customers/merchants are liabilities. Opening Dr clearing 100 / Cr customer 100; purchase Dr customer 80 / Cr M1 80. Final balances 100/20/80/0. All numbers are synthetic PLN minor units. | Uniform signed wallets are simpler to name, but hide the normal-side convention used in later recovery and reconciliation. | Customer balances alone do not sum to zero. Why does the clearing account make the full journal balance? |
| Posting authority | Python owns one explicit transaction and SQL flow. Database checks protect bounded values, identities, terminal records and immutable complete journals. | Whole admission in protected DB functions reduces direct runtime authority, but moves more workflow into SQL. | Omitting the customer projection update still commits. Which independent check catches it, and why does atomicity not? |
| Stable identity and retained results | Workspace-scoped business UUID plus workspace/client/route request key. Keep accepted and rejected results for retained workspace history. | Expiring cached results reduces retained state but permits ambiguous repeats after expiry. | A rejected 80 purchase remains rejected after new funding. Why does a fresh business decision need a new operation ID? |
| Reversal scope | R1 contains purchases; full internal reversal starts in R2. | Including reversal now adds another state/write path before the first path is proven. | R2 must retain multiple rejected attempts while permitting at most one successful full reversal. Why is all-row original-ID uniqueness wrong? |

The implementation choices for R1-PR01 are documented in
[ADR 0002](adr/0002-r1-foundation.md). Sorted account locks at READ COMMITTED remain
the working default for R1-PR02/03; they are not implemented or verified by PR01.
Any later decision packet should contain only the next 3–5 material choices.
