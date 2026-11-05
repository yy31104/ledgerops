# Read-only LangGraph agent

The graph loops `decide -> read -> decide`, then synthesizes a cited answer. Its
five named tools accept an optional business UUID. The model cannot choose a URL,
HTTP method, workspace, credential or write action. The transport issues GET only,
does not follow redirects, and contacts one administrator-configured API origin.

The operator supplies their own token. `/v1/me` establishes the stable workspace,
actor and client. Every tool rechecks the current identity and fixed read scope;
the upstream API is still the authorization authority. A 401 stops the run, 403 is
a scope denial, 404 remains ambiguous, and 429/timeouts have bounded attempts and
backoff. Oversized/malformed responses fail explicitly. Recent lists are bounded.
Credentials live in runtime context and never enter graph state or model prompts.

Limits cap steps, context bytes, output tokens, HTTP attempts and wall time. Billed
API mode requires explicit token prices, reserves an input-byte/framing/output-cap
cost bound before each call and disables automatic inference retries. Failed or
unknown calls retain their reservation. `cost_usd` is an estimated upper bound,
not an invoice. Local subscription use consumes plan quota; it is not a priced API
completion. Cancellation prevents subsequent work; per-provider timeouts bound
ongoing requests and terminate the SDK subprocess group.

Fake models run the same graph and transport guards in CI. A separate acceptance
test uses real Gunicorn, real scoped credentials and disposable PostgreSQL: it
admits 60+2, loses the provider reply and confirms the agent leaves 62 held.

```sh
uv run --project assistant --locked --with-editable . --with gunicorn==26.2.0 \
  python -m pytest -c pyproject.toml -p tests.conftest -q assistant/scenarios/agent_acceptance.py
```

Input injection detection is only a convenience. Closed capabilities and upstream
authorization enforce access. Fake answers measure plumbing and permissions,
not real-model prose quality or semantic entailment.
