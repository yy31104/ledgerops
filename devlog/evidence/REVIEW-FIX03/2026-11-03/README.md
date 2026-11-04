# Review fix03: lease and provider timeout compatibility

The typed worker configuration rejects `lease_seconds * 1000 < provider_timeout_milliseconds + 5000` before provider construction or a worker claim. The same guard applies to `from_env` and direct dataclass construction/replacement. Boundary tests cover the minimum provider deadline, exact1s/5s/30s deadlines and the exact valid/invalid whole-second lease boundary. Existing process fixtures use10s leases for5s provider deadlines and still observe real persisted lease expiry without rewriting the schedule. This is a configuration consistency margin, not a promise that arbitrary database/OS delays can never overlap workers; stable provider identity remains required.

Use the containing commit for this slice and the final integrated delivery evidence for the complete run. The recorded unit suite also includes the contemporaneous queue-fairness call-signature assertion; counts from overlapping runs must not be added as unique cases.
