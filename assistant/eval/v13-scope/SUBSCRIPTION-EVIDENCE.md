# Passage verification with a subscription model

Five local scoring rules missed at least one target on fresh questions. From
round 6, test the opt-in CLI verifier through the official ChatGPT subscription
adapter. It judges whether a supplied passage supports the actual question and
copies a short quote to identify its evidence. The model is `gpt-6.1-sol`, the
model configured by the existing adapter. The HTTP service keeps its local rule.

The unchanged v1.2 reference uses its own runtime router and five-section pack.
The candidate uses the registered informational-question correction, the same
topic and numerical checks, and the same hybrid ranks, with a larger bounded
passage pack for verification and the subsequent CLI answer. Pick the pack on
consumed documentation questions by the number containing a source quote for
every labelled fact. Compare section and byte limits of 5/18,000, 8/28,000,
10/36,000 and 12/42,000. Prefer the smaller pack on ties.

One valid positive verdict is required before answer generation. A malformed
verdict or a quote that does not match the supplied passage is a refusal. Record
invalid verdicts separately. Model request errors consume the attempt and leave
a partial record. Generation after the scope check uses the fake provider.
Record subscription token counters as quota usage. Keep the same frozen cohort
rules, numeric targets, confidence intervals and one-attempt limit.
