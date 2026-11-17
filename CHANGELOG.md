# Changelog

## 1.2.0
- Scope gate: a corpus coverage check refuses questions whose acronyms or specific terms the docs never use.
- New frozen check, run once: 10/34 unsupported questions pass (v1.1 gate: 19/34); 0/54 documentation questions refused.
- Token split: Codex framing and tools were 79% of v1.1 answer input; the subscription adapter now turns those extras off.
- The v1.1 holdout is development data now; v1-007 has a new label version for a fact the sources do not state.
- Limits: unsupported product features worded like the docs still pass; synthetic labels; human answer review is pending.

## 1.1.0
- A reference for testing money-moving code: run `make demo-lost-update` and `make demo-payout-crash`.
- Added isolated guard mutations, a core-module map, and a new frozen assistant comparison with section-cluster intervals.
- Repaired bounded context selection; retained hybrid by the registered rule and preserved refusal failures.
- Limits: synthetic evaluation; 3/10 new unsupported topics pass. Human answer review is pending; cloud remains a recipe.

## 1.0.0
- A synthetic payment ledger with crash recovery and a read-only operator assistant.
- Try `make demo-payout-crash`; `make verify-release` checks durable draft recovery.
- Added cited answers, the local panel, operating guides and release evidence attachments.
- Limits: local validation, finite filters and AI-rated answers; human review is pending. No cloud runtime or cost measurement.
