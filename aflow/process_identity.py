"""Portable process-birth identities for ownership checks."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Literal


ProcessLiveness = Literal["present", "absent", "unknown"]


def _linux_procfs_is_usable() -> bool:
    """Return whether the current procfs exposes a parseable stat shape."""
    try:
        stat = Path("/proc/self/stat").read_text(encoding="utf-8")
    except (OSError, IndexError, UnicodeError):
        return False
    closing = stat.rfind(")")
    if closing < 0 or stat[closing + 1 : closing + 2] != " ":
        return False
    return len(stat[closing + 2 :].split()) > 19


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
    except FileNotFoundError:
        if sys.platform == "linux" and _linux_procfs_is_usable():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None
            except OSError:
                pass
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


def process_liveness(pid: int) -> ProcessLiveness:
    """Return only liveness evidence, keeping identity-observation gaps unknown.

    A missing birth identity is not enough to release an owner reservation: the
    process may still exist while procfs or ``ps`` cannot expose its birth
    data.  This separate probe therefore reports confirmed absence only when
    the operating system positively says that the PID is gone (or the process
    is a zombie); all observation failures remain ``unknown``.
    """
    if pid < 1:
        return "absent"

    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            closing = stat.rfind(")")
            suffix = stat[closing + 2 :].split()
            if closing < 0 or stat[closing + 1 : closing + 2] != " " or not suffix:
                raise ValueError("malformed process stat")
            return "absent" if suffix[0] == "Z" else "present"
        except FileNotFoundError:
            if _linux_procfs_is_usable():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return "absent"
                except PermissionError:
                    return "present"
                except OSError:
                    pass
                else:
                    return "present"
        except (OSError, IndexError, UnicodeError, ValueError):
            pass

    try:
        completed = subprocess.run(
            ("ps", "-o", "pid=", "-p", str(pid)),
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode == 0 and completed.stdout.strip():
        return "present"
    if completed.returncode != 0 and not completed.stdout.strip() and not completed.stderr.strip():
        return "absent"
    return "unknown"
