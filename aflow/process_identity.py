"""Portable process-birth identities for ownership checks."""

from __future__ import annotations

from pathlib import Path
import subprocess


def process_birth_identity(pid: int) -> str | None:
    """Return a stable process-birth identity, not merely a reusable PID."""
    if pid < 1:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = stat[stat.rfind(")") + 2 :].split()
        if suffix[0] == "Z":
            return None
        return f"linux-start-ticks:{suffix[19]}"
    except (OSError, IndexError):
        pass
    completed = subprocess.run(
        ("ps", "-o", "lstart=", "-p", str(pid)),
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    return f"ps-lstart:{value}" if completed.returncode == 0 and value else None
