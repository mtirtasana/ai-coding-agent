"""Optional, provider-neutral, read-only MCP surface; no global plugin rules."""

import argparse
import json
from pathlib import Path

from agent.adapters.artifacts import ArtifactStore
from agent.adapters.tools import workspace_tools
from agent.core.types import ToolCall
from agent.files import Workspace


def build_server(workspace, artifacts, factory=None):
    if factory is None:
        from mcp.server.fastmcp import FastMCP

        factory = FastMCP
    server = factory("generic-coding-agent")
    tools = workspace_tools(workspace, archive=artifacts)

    @server.tool()
    def batch_inspect(requests: list[dict]) -> dict:
        """Inspect up to ten guarded ranges; hashes are historical and the batch is not atomic."""
        result = tools.execute(
            ToolCall("mcp-batch", "batch_inspect", json.dumps({"requests": requests}))
        )
        return {
            "ok": result.ok,
            "result": json.loads(result.content) if result.ok else result.content,
        }

    @server.tool()
    def archive_read(handle: str, offset: int, length: int) -> dict:
        """Retrieve up to 4096 archived bytes as base64; never execute or replay operations."""
        return json.loads(artifacts.read(handle, offset, length))

    return server


def main():
    parser = argparse.ArgumentParser(
        description="Read-only generic coding agent MCP server over stdio."
    )
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--persist-artifacts", action="store_true")
    args = parser.parse_args()
    workspace = Workspace(args.workspace)
    archive = ArtifactStore(workspace, persistent=args.persist_artifacts)
    build_server(workspace, archive).run(transport="stdio")


if __name__ == "__main__":
    main()
