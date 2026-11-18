# Changelog

## 1.3.1
- Add optional ChatGPT subscription passage verification to the local HTTP assistant.
- On 204 frozen questions, the verified profile admitted 1/102 unsupported questions and refused 2/102 documentation questions. Both HTTP profiles had Recall@5 of 0.8464.
- The default Compose demo uses the offline rule. Median HTTP latency was 0.36 s offline and 5.88 s with verification.

## 1.3.0
- Recognize informational question openings before checking passage support.
- Add the optional local CLI subscription verifier.
- In the frozen CLI evaluation, it admitted 1/108 (0.9%) unsupported questions and refused 4/103 (3.9%) documentation questions.
- Hybrid Recall@5 was 0.9110 for both gates. The HTTP service uses the local scoring rule.

## 1.2.0
- Scope gate: a corpus coverage check refuses questions whose acronyms or specific terms the docs never use.
- New frozen check, run once: 10/34 unsupported questions pass (v1.1 gate: 19/34); 0/54 documentation questions refused.
- Token split: Codex framing and tools were 79% of v1.1 answer input; the subscription adapter now turns those extras off.
- The v1.1 holdout is development data now; v1-007 has a new label version for a fact the sources do not state.
- Limits: some unsupported features still pass when they use terms from the docs.

## 1.1.0
- A reference for testing money-moving code: run `make demo-lost-update` and `make demo-payout-crash`.
- Added isolated guard mutations, a core-module map, and a new frozen assistant comparison with section-cluster intervals.
- Repaired bounded context selection; retained hybrid by the registered rule and preserved refusal failures.
- Limits: synthetic evaluation; 3/10 new unsupported topics pass. Cloud setup is documented.

## 1.0.0
- A synthetic payment ledger with crash recovery and a read-only operator assistant.
- Try `make demo-payout-crash`; `make verify-release` checks durable draft recovery.
- Added cited answers, the local panel, operating guides and release evidence attachments.
- Limits: local validation and synthetic evaluation. No cloud runtime or cost measurement.
