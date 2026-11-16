"""Provider-reported usage, separate from the conservative budget reservation."""


def accumulate(state, usage, called):
    if not called:
        return {}
    usage = usage if isinstance(usage, dict) else {}
    counts = {key: usage.get(key) for key in ("input_tokens", "output_tokens")}
    valid = {key: type(value) is int and value >= 0 for key, value in counts.items()}
    complete = all(valid.values())
    return {
        **{key: state.get(key, 0) + (value if valid[key] else 0) for key, value in counts.items()},
        "model_calls": state.get("model_calls", 0) + 1,
        "reported_model_calls": state.get("reported_model_calls", 0) + int(complete),
        "usage_complete": state.get("usage_complete", True) and complete,
    }


def summary(state, provider, limits):
    complete = state.get("usage_complete", True)
    input_tokens, output_tokens = state.get("input_tokens", 0), state.get("output_tokens", 0)
    billing = (
        "subscription_quota"
        if provider == "chatgpt_subscription"
        else "token_priced"
        if provider.startswith("openai:")
        else "synthetic"
        if provider == "fake"
        else "local"
        if provider.startswith("ollama:")
        else "unknown"
    )
    return {
        "provider": provider,
        "billing": billing,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "complete": complete,
        "model_calls": state.get("model_calls", 0),
        "reported_model_calls": state.get("reported_model_calls", 0),
        # Provider counters, including any SDK harness overhead, are not an invoice.
        # Missing counters cannot be presented as zero cost. Subscription quota has
        # no per-request dollar price; API estimates use configured uncached rates.
        "token_cost_estimate_usd": (
            (
                input_tokens * limits.input_usd_per_million
                + output_tokens * limits.output_usd_per_million
            )
            / 1e6
            if complete and billing == "token_priced"
            else None
        ),
        "cost_basis": "configured_uncached_token_prices" if billing == "token_priced" else billing,
    }
