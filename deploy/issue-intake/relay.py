#!/usr/bin/env python3
"""Convert one owner-authorized GitHub issue event into an intake envelope.

This is deliberately not an Action or a service. An operator invokes it from a
reviewed workflow after selecting a private transport. It never forwards issue
text, and the host command performs independent canonical GitHub checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence


OWNER_ID = 591691
MAX_EVENT_BYTES = 256 * 1024
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")


class RelayFailure(ValueError):
    """The event or operator-provided transport is unsuitable for relay."""


def source_hash(title: str, body: str | None) -> str:
    encoded = json.dumps(
        [title, body or ""], ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_id(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RelayFailure(f"{label}_invalid")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RelayFailure(f"{label}_invalid")
    return value


def _text(value: object, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise RelayFailure(f"{label}_invalid")
    return value


def _read_event(path: Path) -> Mapping[str, object]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RelayFailure("event_unavailable") from exc
    if len(data) > MAX_EVENT_BYTES:
        raise RelayFailure("event_too_large")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RelayFailure("event_invalid_json") from exc
    return _mapping(value, "event")


def event_envelope(event: Mapping[str, object], *, event_name: str, run_id: str) -> dict[str, object]:
    """Return the metadata-only envelope, rejecting every non-owner event."""
    if event_name != "issues":
        raise RelayFailure("event_kind_rejected")
    if not RUN_ID_RE.fullmatch(run_id):
        raise RelayFailure("run_id_invalid")
    action = _text(event.get("action"), "action")
    if action not in {"opened", "edited"}:
        raise RelayFailure("event_action_rejected")
    repository = _mapping(event.get("repository"), "repository")
    issue = _mapping(event.get("issue"), "issue")
    sender = _mapping(event.get("sender"), "sender")
    author = _mapping(issue.get("user"), "issue_author")
    repository_id = _positive_id(repository.get("id"), "repository_id")
    issue_number = _positive_id(issue.get("number"), "issue_number")
    actor_id = _positive_id(sender.get("id"), "actor_id")
    author_id = _positive_id(author.get("id"), "author_id")
    full_name = _text(repository.get("full_name"), "repository_full_name")
    title = _text(issue.get("title"), "issue_title")
    body = _text(issue.get("body"), "issue_body", allow_none=True)
    assert full_name is not None and title is not None
    if actor_id != OWNER_ID or author_id != OWNER_ID:
        raise RelayFailure("owner_admission_rejected")
    return {
        "repository_id": repository_id,
        "repository_full_name": full_name,
        "issue_number": issue_number,
        "actor_id": actor_id,
        "action": action,
        "delivery_id": run_id,
        "title_body_sha256": source_hash(title, body),
    }


def _path(value: str, label: str) -> str:
    candidate = Path(value)
    if not candidate.is_absolute() or "\x00" in value:
        raise RelayFailure(f"{label}_invalid")
    return value


def host_command(args: argparse.Namespace) -> list[str]:
    command = _path(args.host_command, "host_command")
    config = _path(args.config, "config")
    if args.ssh_host is None:
        return [command, "--config", config, "--event-file", "-"]
    if not HOST_RE.fullmatch(args.ssh_host) or not USER_RE.fullmatch(args.ssh_user or ""):
        raise RelayFailure("ssh_target_invalid")
    identity = _path(args.ssh_identity_file, "ssh_identity_file")
    return [
        "ssh",
        "-i",
        identity,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        f"{args.ssh_user}@{args.ssh_host}",
        command,
        "--config",
        config,
        "--event-file",
        "-",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Relay owner issue metadata to a fixed private intake command")
    parser.add_argument("--host-command", required=True, help="operator-approved absolute intake command")
    parser.add_argument("--config", required=True, help="absolute host-side intake TOML path")
    parser.add_argument("--ssh-host", help="operator-approved private SSH host")
    parser.add_argument("--ssh-user", help="operator-approved private SSH user")
    parser.add_argument("--ssh-identity-file", help="absolute private SSH identity path")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    run: Any = subprocess.run,
) -> int:
    args = build_parser().parse_args(argv)
    environment = os.environ if environ is None else environ
    try:
        event_path = environment.get("GITHUB_EVENT_PATH")
        run_id = environment.get("GITHUB_RUN_ID")
        event_name = environment.get("GITHUB_EVENT_NAME")
        if not event_path or not run_id or not event_name:
            raise RelayFailure("github_environment_missing")
        envelope = event_envelope(_read_event(Path(event_path)), event_name=event_name, run_id=run_id)
        payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        run(host_command(args), input=payload, check=True)
    except (RelayFailure, OSError, subprocess.CalledProcessError) as exc:
        print(f"issue-intake relay: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
