import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ledgerops_assistant.mcp_server import build_server, private_token


def test_private_token_file_rejects_world_readable_files_and_symlinks(tmp_path):
    path = tmp_path / "credential.json"
    path.write_text('{"token":"synthetic"}')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        private_token(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        private_token(link)


@pytest.mark.parametrize("provider", [None, "fake", "openai_api", "ollama"])
def test_real_stdio_mcp_roundtrip_has_no_credential_argument(tmp_path, provider):
    token = "lo1_" + "m" * 43
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"token": token}))
    path.chmod(0o600)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/assist"
            assert self.headers.get("Authorization") == "Bearer " + token
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert set(data) == {"question", "request_id", "provider"}
            assert data["provider"] == (provider or "fake")
            if data["question"] == "Denied access":
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = {
                "answer": {"text": "Checked fixture evidence", "citations": []},
                "cost_usd": 0,
            }
            body = ("event: result\ndata: " + json.dumps(payload) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    async def run():
        env = {k: v for k, v in os.environ.items() if k != "LEDGEROPS_ASSISTANT_MCP_PROVIDER"}
        if provider is not None:
            env["LEDGEROPS_ASSISTANT_MCP_PROVIDER"] = provider
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ledgerops_assistant.mcp_server"],
            env={
                **env,
                "LEDGEROPS_ASSISTANT_URL": f"http://127.0.0.1:{server.server_port}",
                "LEDGEROPS_ASSISTANT_TOKEN_FILE": str(path),
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == ["investigate"]
            assert set(tools.tools[0].inputSchema["properties"]) == {"question"}
            result = await session.call_tool("investigate", {"question": "Investigate timeout"})
            assert result.structuredContent["answer"]["text"] == "Checked fixture evidence"
            assert token not in result.model_dump_json()
            denied = await session.call_tool("investigate", {"question": "Denied access"})
            assert denied.structuredContent == {
                "refused": True,
                "reason": "assistant_access_unavailable",
            }
            assert token not in denied.model_dump_json()

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.mark.parametrize("provider", ["chatgpt_subscription", "", "arbitrary", "https://invalid"])
def test_mcp_rejects_unavailable_provider_configuration(monkeypatch, provider):
    monkeypatch.setenv("LEDGEROPS_ASSISTANT_MCP_PROVIDER", provider)
    with pytest.raises(ValueError, match="MCP provider"):
        build_server(token_file="unused-private-file")
