"""SDK isolation process: never used in CI or by the HTTP service."""

import json
import os
import sys
import tempfile
from collections import Counter

BASE_OVERRIDES = (
    'web_search="disabled"',
    "features.shell_tool=false",
    "features.unified_exec=false",
    "mcp_servers={}",
    "apps._default.enabled=false",
)
# Codex otherwise adds the local user's skill list, plugin suggestions, collaboration
# and permission notes, environment details and their tools to every request.
ISOLATION_OVERRIDES = (
    "skills.include_instructions=false",
    "include_collaboration_mode_instructions=false",
    "include_permissions_instructions=false",
    "include_environment_context=false",
    "features.plugins=false",
    "features.apps=false",
    "features.multi_agent=false",
    "features.image_generation=false",
    "features.computer_use=false",
    "features.browser_use=false",
    "features.goals=false",
    "features.tool_suggest=false",
    "features.view_image=false",
)


def overrides(isolated: bool) -> tuple[str, ...]:
    return BASE_OVERRIDES + (ISOLATION_OVERRIDES if isolated else ())


def breakdown(usage) -> dict:
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cached_input_tokens": getattr(usage, "cached_input_tokens", None),
        "reasoning_output_tokens": getattr(usage, "reasoning_output_tokens", None),
    }


def main():
    if os.environ.get("CI") or os.environ.get("LEDGEROPS_ASSISTANT_SERVER"):
        raise ValueError("subscription worker is disabled in CI and servers")
    if os.environ.get("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION") != "1":
        raise ValueError("subscription use requires explicit opt in")
    from openai_codex import ApprovalMode, Codex, CodexConfig, ExternalMessage, Sandbox

    request = json.load(sys.stdin)
    with tempfile.TemporaryDirectory(prefix="ledgerops-model-") as directory:
        isolated = bool(request.get("isolated", True))
        config = CodexConfig(cwd=directory, config_overrides=overrides(isolated))
        with Codex(config) as codex:
            account = codex.account().account
            account = getattr(account, "root", account)
            if account is None or getattr(account, "type", None) != "chatgpt":
                raise ValueError("ChatGPT sign-in is required; API-key authentication is rejected")
            thread = codex.thread_start(
                cwd=directory,
                ephemeral=True,
                model_provider="openai",
                model=request.get("model", "gpt-6.1-sol"),
                sandbox=Sandbox.read_only,
                approval_mode=ApprovalMode.deny_all,
                base_instructions=request["instructions"],
                developer_instructions="Answer using supplied evidence. Do not call tools.",
            )
            result = thread.run(
                ExternalMessage(tool_name="ledgerops_evidence", content=request["message"]),
                output_schema=request["schema"],
                approval_mode=ApprovalMode.deny_all,
                # Short schema-constrained turns must fit the agent's call deadline.
                effort="low",
            )
            if str(getattr(result.status, "value", result.status)) != "completed":
                raise RuntimeError("Codex turn did not complete")
            total = getattr(result.usage, "total", None)
            items = Counter(
                type(getattr(i, "root", i)).__name__ for i in getattr(result, "items", [])
            )
            json.dump(
                {
                    "text": result.final_response,
                    "usage": {
                        **breakdown(total),
                        # The last model request alone; differs from the total when
                        # the turn made more than one request.
                        "last_request": breakdown(getattr(result.usage, "last", None)),
                        "turn_items": dict(sorted(items.items())),
                        "isolated": isolated,
                        "model": request.get("model", "gpt-6.1-sol"),
                        "instruction_utf8_bytes": len(request["instructions"].encode()),
                        "message_utf8_bytes": len(request["message"].encode()),
                        "schema_utf8_bytes": len(json.dumps(request["schema"]).encode()),
                        "billing": "chatgpt_plan",
                    },
                },
                sys.stdout,
            )


if __name__ == "__main__":
    main()
