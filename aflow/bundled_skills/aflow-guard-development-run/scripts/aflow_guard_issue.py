#!/usr/bin/env python3
"""Validate, deduplicate, and optionally create one sanitized AFlow defect issue."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any, Iterable

REPOSITORY = "evrenesat/aworkflow"
REQUIRED_FIELDS = (
    "title",
    "fingerprint",
    "aflow_version",
    "expected",
    "actual",
    "impact",
    "reproduction",
    "evidence",
    "workaround",
)
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
FORBIDDEN_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|authorization|bearer|password|secret|token)\b"
        r"\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|\s)/(?:Users|home|root|opt|srv|var)/\S+"),
    re.compile(r"\b[A-Za-z]:\\Users\\\S+", re.IGNORECASE),
    re.compile(r"https?://\S+", re.IGNORECASE),
)
FORBIDDEN_FIELD_NAMES = {
    "project",
    "project_name",
    "repository",
    "repository_path",
    "plan",
    "plan_path",
    "prompt",
    "branch",
    "branch_name",
    "user",
    "username",
}


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def validate_payload(payload: dict[str, Any]) -> dict[str, str]:
    forbidden = FORBIDDEN_FIELD_NAMES.intersection(payload)
    if forbidden:
        raise ValueError(
            "project-identifying fields are forbidden: " + ", ".join(sorted(forbidden))
        )
    missing = [
        field
        for field in REQUIRED_FIELDS
        if not isinstance(payload.get(field), str) or not payload[field].strip()
    ]
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))
    fingerprint = payload["fingerprint"].strip().lower()
    if not FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise ValueError("fingerprint must be 12-64 lowercase hexadecimal characters")

    redaction_terms = payload.get("redaction_terms", [])
    if not isinstance(redaction_terms, list) or not all(
        isinstance(item, str) and item.strip() for item in redaction_terms
    ):
        raise ValueError("redaction_terms must be a list of non-empty strings")
    content = "\n".join(_strings({key: payload[key] for key in REQUIRED_FIELDS}))
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(content):
            raise ValueError("issue content contains sensitive or project-identifying data")
    lowered = content.casefold()
    for term in redaction_terms:
        if term.casefold() in lowered:
            raise ValueError("issue content contains a supplied redaction term")

    return {
        field: payload[field].strip()
        for field in REQUIRED_FIELDS
    } | {"fingerprint": fingerprint}


def render_issue(payload: dict[str, str]) -> tuple[str, str, str]:
    marker = f"aflow-guard-fingerprint:{payload['fingerprint']}"
    title = f"[AFlow guard] {payload['title']}"
    body = f"""<!-- {marker} -->

## AFlow version

{payload['aflow_version']}

## Expected behavior

{payload['expected']}

## Actual behavior

{payload['actual']}

## Impact

{payload['impact']}

## Minimal reproduction

{payload['reproduction']}

## Bounded evidence

{payload['evidence']}

## Workaround status

{payload['workaround']}
"""
    return title, body, marker


def _gh_json(args: list[str]) -> Any:
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "GitHub CLI failed")
    return json.loads(completed.stdout or "null")


def find_existing(marker: str) -> dict[str, Any] | None:
    result = _gh_json(
        [
            "issue",
            "list",
            "--repo",
            REPOSITORY,
            "--state",
            "all",
            "--search",
            f"{marker} in:body",
            "--limit",
            "10",
            "--json",
            "number,title,url,state",
        ]
    )
    if not isinstance(result, list):
        raise RuntimeError("unexpected GitHub issue search response")
    return result[0] if result else None


def create_issue(title: str, body: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            REPOSITORY,
            "--title",
            title,
            "--body",
            body,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "GitHub issue creation failed")
    return {"created": True, "url": completed.stdout.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--create", action="store_true")
    args = parser.parse_args()

    try:
        raw = json.load(sys.stdin)
        if not isinstance(raw, dict):
            raise ValueError("input must be one JSON object")
        payload = validate_payload(raw)
        title, body, marker = render_issue(payload)
        if not args.create:
            print(json.dumps({"valid": True, "title": title, "body": body}))
            return 0
        existing = find_existing(marker)
        if existing is not None:
            print(json.dumps({"created": False, "existing": existing}))
            return 0
        print(json.dumps(create_issue(title, body)))
        return 0
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
