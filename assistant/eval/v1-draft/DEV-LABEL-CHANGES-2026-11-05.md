# Development label review for current main documentation

Scope: retain the existing questions, splits, groups, key facts and refusal labels.
Review the 32 development documentation questions against changed sections in
README.md and ADR 0004 at main d57f04a4e4135deb83e2b83cfb06b75634361640.
No ranking results were used to choose these additions. No v2 holdout annotation
or result is changed. These remain synthetic labels without independent human
validation.

The new sufficient section is:
docs/adr/0004-contention-and-recovery.md#payout-dispatch-without-a-distributed-transaction

| Question | Addition and source reason |
|---|---|
| v1-001 | Add the section. The tentative-operation row says the hold is consumed only on confirmed success and released only on confirmed failure or a confirmed close without payment. The rejected alternative explicitly warns against releasing after a timeout. Both existing required facts are stated. |
| v1-002 | Add the section. The uncertainty row says recovery starts with LOOKUP using the same key. The evidence table states abandoned claims recover by LOOKUP and includes test_abandoned_claim_is_due_after_lease_and_recovers_by_lookup_not_new_put. The lease qualification is stated in that linked test identifier, not repeated in the prose. |
| v1-004 | Add the section. The sender row says claim_payout commits MAY_HAVE_SENT and UNKNOWN before the network call; death after that commit means the payment may exist. The rejected send-first alternative explains losing knowledge when a process dies before recording. |
| v1-005 | Add the section. The sender row defines a post-commit crash as an unknown outcome in which the payment may exist. The uncertainty row resolves uncertainty by LOOKUP with the same key, rather than assuming not paid. |

No existing relevance section is removed. The other 28 development documentation
questions receive no relevance changes. In particular, v1-003 also requires the
in-flight PUT-before-closure race, which this new section does not explicitly
state; v1-011 requires the 80/0/80 result and no outgoing journal, which it does not
state. General relatedness to dispatch is insufficient. The new section
does not state the required facts for admission, response-code distinctions,
quarantine, operator approval, reconciliation, maintenance or metrics questions.

The changed README failure-cases paragraph describes concurrent admission attacks
and their lock experiment. The changed Limits and Docs sections describe
deployment coverage and links. None adds a sufficient section for all required
facts of another development question. Links alone are not sufficient labels.

## Version binding

- Previous complete question-file SHA-256:
  5c4c6a454d54da93512d56dbd0840cca0a46022d4ac23a79db35a39b4e7695e5.
- Updated question-file SHA-256:
  1f1ac04b54f87a8e4524a12375d2fca4654b530785a7f7f2d84bed681f3cc94c.
- Only four development relevant_sections lists change. All question wording,
  key_facts, routes and test-split records are unchanged.
- The prior baseline remains eval/results/v2-postgres-dev.json as historical
  evidence; current CI uses a separate main-docs baseline.
