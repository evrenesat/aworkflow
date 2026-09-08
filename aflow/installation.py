"""Resolve properties of the executing AFlow installation.

``aflow ui`` must work from any directory without environment ceremony: it
resolves the installed executable and release identity from the installation
that is executing the command, and detects editable development installs
through distribution metadata (``direct_url.json``), never from the current
working directory. An unrelated active virtual environment therefore cannot
select a different AFlow installation.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys


@dataclass(frozen=True)
class Installation:
    kind: str  # "wheel" | "editable" | "unknown"
    checkout_root: Path | None
    release_identity: str
    executable: Path

    @property
    def editable(self) -> bool:
        return self.kind == "editable"


def resolve_executable() -> Path:
    """Resolve an executable aflow entry point for child/worker re-exec."""
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name != "python" and os.access(candidate, os.X_OK) and candidate.is_file():
        return candidate
    discovered = shutil.which("aflow")
    if discovered:
        return Path(discovered).resolve()
    return candidate


def release_identity() -> str:
    try:
        return importlib.metadata.version("aworkflow")
    except Exception:
        return "dev"


def _distribution_url(dist: importlib.metadata.Distribution) -> Path | None:
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    editable = payload.get("dir_info", {}).get("editable", False)
    if not editable:
        return None
    return Path(url[len("file://") :])


def detect_installation(distribution_name: str = "aworkflow") -> Installation:
    """Detect the installation kind and, for editable installs, its checkout."""
    try:
        dist = importlib.metadata.distribution(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return Installation(
            kind="unknown",
            checkout_root=None,
            release_identity=release_identity(),
            executable=resolve_executable(),
        )
    url = _distribution_url(dist)
    checkout: Path | None = None
    if url is not None:
        # direct_url.json records the install path; the checkout root is the
        # directory containing the aflow package (validated against manifests).
        for candidate in (url, *url.parents):
            if (candidate / "aflow" / "cli.py").is_file() and (
                candidate / "pyproject.toml"
            ).is_file():
                checkout = candidate
                break
        if checkout is None and (url / "aflow" / "cli.py").is_file():
            checkout = url
    return Installation(
        kind="editable" if checkout is not None else "wheel",
        checkout_root=checkout,
        release_identity=release_identity(),
        executable=resolve_executable(),
    )
