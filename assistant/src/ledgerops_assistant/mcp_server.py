"""Local stdio MCP adapter. The caller token is never an MCP tool argument."""

import json
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


def private_token(path):
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > 16384
        ):
            raise ValueError("credential file must be owner-private and regular")
        raw = os.read(fd, 16385)
    finally:
        os.close(fd)
    data = json.loads(raw)
    token = data.get("token")
    if not isinstance(token, str):
        raise ValueError("credential file must contain token")
    return token


def build_server(origin=None, token_file=None):
    origin = origin or os.environ.get("LEDGEROPS_ASSISTANT_URL", "http://127.0.0.1:8088")
    url = urlsplit(origin)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("configure a fixed assistant origin")
    if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("plaintext MCP transport is local only")
    token_file = token_file or os.environ.get("LEDGEROPS_ASSISTANT_TOKEN_FILE")
    if not token_file:
        raise ValueError("set LEDGEROPS_ASSISTANT_TOKEN_FILE to an owner-private credential file")
    server = FastMCP("LedgerOps operator assistant")

    @server.tool(
        structured_output=True,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def investigate(question: str) -> dict[str, Any]:
        """Investigate a LedgerOps question with read-only tools and checked citations."""
        if not 1 <= len(question) <= 8000:
            return {"refused": True, "reason": "question_length"}
        try:
            token = private_token(token_file)
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=90, trust_env=False
            ) as client:
                async with client.stream(
                    "POST",
                    origin.rstrip("/") + "/v1/assist",
                    headers={"Authorization": "Bearer " + token},
                    json={"question": question, "request_id": str(uuid4()), "provider": "fake"},
                ) as response:
                    if response.status_code != 200:
                        return {"refused": True, "reason": "assistant_access_unavailable"}
                    event, total = "", 0
                    async for line in response.aiter_lines():
                        total += len(line)
                        if total > 2_000_000:
                            return {"refused": True, "reason": "response_limit"}
                        if line.startswith("event: "):
                            event = line[7:]
                        elif line.startswith("data: ") and event == "result":
                            return json.loads(line[6:])
            return {"refused": True, "reason": "incomplete_response"}
        except Exception:
            return {"refused": True, "reason": "assistant_unavailable"}

    return server


if __name__ == "__main__":
    build_server().run(transport="stdio")
