"""SDK isolation process: never used in CI or by the HTTP service."""

import json
import os
import sys
import tempfile


def main():
    if os.environ.get("CI") or os.environ.get("LEDGEROPS_ASSISTANT_SERVER"):
        raise ValueError("subscription worker is disabled in CI and servers")
    if os.environ.get("LEDGEROPS_ASSISTANT_ALLOW_SUBSCRIPTION") != "1":
        raise ValueError("subscription use requires explicit opt in")
    from openai_codex import ApprovalMode, Codex, CodexConfig, ExternalMessage, Sandbox

    request = json.load(sys.stdin)
    with tempfile.TemporaryDirectory(prefix="ledgerops-model-") as directory:
        config = CodexConfig(
            cwd=directory,
            config_overrides=(
                'web_search="disabled"',
                "features.shell_tool=false",
                "features.unified_exec=false",
                "mcp_servers={}",
                "apps._default.enabled=false",
            ),
        )
        with Codex(config) as codex:
            account = codex.account().account
            account = getattr(account, "root", account)
            if account is None or getattr(account, "type", None) != "chatgpt":
                raise ValueError("ChatGPT sign-in is required; API-key authentication is rejected")
            thread = codex.thread_start(
                cwd=directory,
                ephemeral=True,
                model_provider="openai",
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
            usage = getattr(result.usage, "total", None)
            json.dump(
                {
                    "text": result.final_response,
                    "usage": {
                        "input_tokens": getattr(usage, "input_tokens", None),
                        "output_tokens": getattr(usage, "output_tokens", None),
                        "billing": "chatgpt_plan",
                    },
                },
                sys.stdout,
            )


if __name__ == "__main__":
    main()
