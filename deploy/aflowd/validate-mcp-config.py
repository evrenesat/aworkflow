#!/usr/bin/env python3
"""Fail closed on Mac MCP configuration that could leak a bearer token."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.11+ is required by AFlow.
    import tomli as tomllib


WRITE_TOOLS = ("start_run", "answer_startup", "control_run", "owner_stop", "resume_run")


def validate(path: Path) -> None:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers table is required")
    server = servers.get("aflow_control_plane")
    if not isinstance(server, dict):
        raise ValueError("aflow_control_plane MCP server is required")
    url = server.get("url")
    if not isinstance(url, str):
        raise ValueError("MCP URL is required")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "100.103.69.9"
        or parsed.port != 8765
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("MCP URL must be the credential-free Tailscale /mcp endpoint")
    if server.get("required") is not False:
        raise ValueError("MCP server must remain optional")
    if not isinstance(server.get("bearer_token_env_var"), str) or not server["bearer_token_env_var"]:
        raise ValueError("MCP bearer token must be supplied by an environment variable")
    if server.get("default_tools_approval_mode") != "writes":
        raise ValueError("MCP write approval mode must be enabled")
    tools = server.get("tools")
    if not isinstance(tools, dict):
        raise ValueError("MCP write tool approval configuration is required")
    for name in WRITE_TOOLS:
        tool = tools.get(name)
        if not isinstance(tool, dict) or tool.get("approval_mode") != "approve":
            raise ValueError(f"MCP tool {name} must require approval")


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if len(arguments) != 1:
        print("usage: validate-mcp-config.py PATH", file=sys.stderr)
        return 2
    try:
        validate(Path(arguments[0]))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"invalid aflow control-plane MCP configuration: {exc}", file=sys.stderr)
        return 1
    print("aflow control-plane MCP configuration is credential-free and approval-gated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
