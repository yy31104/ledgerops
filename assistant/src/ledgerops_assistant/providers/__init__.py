"""Answer providers, chosen by name like a provider setting in an editor plugin.

| Name | What it is | Use it for |
|---|---|---|
| fake | Deterministic sentence copier | CI and tests; never spends money |
| openai_api | OpenAI API key, billed per token | Evaluation runs and demos |
| ollama | Local model through Ollama | Offline experiments |
| chatgpt_subscription | ChatGPT plan via official Python SDK | Local-only, explicit opt-in |
"""

from ledgerops_assistant.providers.base import Draft, Provider


def get_provider(name: str) -> Provider:
    if name == "fake":
        from ledgerops_assistant.providers.fake import FakeProvider

        return FakeProvider()
    if name == "openai_api":
        from ledgerops_assistant.providers.openai_api import OpenAIProvider

        return OpenAIProvider()
    if name == "ollama":
        from ledgerops_assistant.providers.ollama import OllamaProvider

        return OllamaProvider()
    if name == "chatgpt_subscription":
        from ledgerops_assistant.providers.subscription import SubscriptionProvider

        return SubscriptionProvider()
    raise ValueError(f"unknown provider {name!r}")


__all__ = ["Draft", "Provider", "get_provider"]
