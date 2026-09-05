#!/usr/bin/env python3
"""Validate the private Tailscale Serve mapping without assuming a node name."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            result.append(str(key))
            result.extend(_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_strings(item))
        return result
    return [str(value)]


def validate(serve_path: Path, tailscale_path: Path) -> str:
    serve = json.loads(serve_path.read_text(encoding="utf-8"))
    tailscale = json.loads(tailscale_path.read_text(encoding="utf-8"))
    serve_text = "\n".join(_strings(serve)).lower()
    if "127.0.0.1:8765" not in serve_text:
        raise ValueError("Serve status does not target 127.0.0.1:8765")
    if "443" not in serve_text or "https" not in serve_text:
        raise ValueError("Serve status does not expose private HTTPS on port 443")
    if "funnel" in serve_text and any(marker in serve_text for marker in ("true", "on", "enabled")):
        raise ValueError("Serve status indicates Funnel exposure")
    self_record = tailscale.get("Self")
    if not isinstance(self_record, dict):
        raise ValueError("Tailscale status has no Self record")
    dns_name = self_record.get("DNSName")
    if not isinstance(dns_name, str) or not dns_name.rstrip(".").endswith(".ts.net"):
        raise ValueError("Tailscale status has no MagicDNS name")
    return f"https://{dns_name.rstrip('.')}/"


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if len(arguments) != 2:
        print("usage: validate-serve-status.py SERVE_STATUS_JSON TAILSCALE_STATUS_JSON", file=sys.stderr)
        return 2
    try:
        url = validate(Path(arguments[0]), Path(arguments[1]))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid private Serve status: {exc}", file=sys.stderr)
        return 1
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
