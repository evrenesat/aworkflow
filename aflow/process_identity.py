"""Portable process-birth identities for ownership checks.

This module also owns the session-scoped process topology helpers used by
the exclusive execution lifecycle:

- :func:`session_members` observes one session's members in a single bounded
  pass and returns ``None`` (unknown) on any ambiguity.  It never signals
  anything.
- :func:`session_member_live` revalidates one captured member against the
  recorded birth identity, session id, and process group before it may be
  treated as live or signalled.
- :func:`process_group_state` reports a positive group state (absent,
  present, unknown) used for lease termination evidence.
- :func:`terminate_owned_group` performs the bounded TERM/KILL escalation of
  one verified owned group and returns ``True`` only on positive group
  absence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
import signal
from pathlib import Path
import subprocess
import sys
import time
from typing import Literal


ProcessLiveness = Literal["present", "absent", "unknown"]
GroupState = Literal["absent", "present", "unknown"]

#: Hard bounds for a single session observation pass.
SESSION_MAX_MEMBERS = 4096
SESSION_MAX_BYTES = 1_048_576
SESSION_OBSERVATION_SECONDS = 2.0


@dataclass(frozen=True)
class SessionMember:
    """One captured session member with its birth-validated identity."""

    pid: int
    birth: str | None
    pgid: int


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
    try:
        completed = subprocess.run(
            ("ps", "-o", "lstart=", "-p", str(pid)),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return f"ps-lstart:{value}" if completed.returncode == 0 and value else None


def host_boot_identity() -> str | None:
    """Return a portable identity of the current host boot, if observable.

    Linux exposes a random boot UUID under procfs; macOS exposes a boot
    session UUID through ``sysctl``.  A missing or unobservable value is
    ``None`` so callers fail closed instead of assuming no reboot occurred.
    """
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None
    if sys.platform == "linux":
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value if value else None
    return None


def _bounded_probe_timeout(deadline: float) -> float | None:
    """The bounded window left on a shared stop deadline, if any remains.

    The window is the raw remaining time (capped at the two-second
    observation bound) with no minimum floor: a floor that exceeds the
    remaining stop budget would let one probe spend time the shared deadline
    no longer owns.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(SESSION_OBSERVATION_SECONDS, remaining)


def process_birth_bounded(pid: int, deadline: float) -> str | None:
    """Bounded birth observation for deadline-bound stop paths.

    Linux uses procfs only; Darwin uses a single ``ps`` birth probe whose
    timeout is the smaller of the shared two-second observation window and
    the remaining outer deadline.  Never falls back to the generic five-second
    probe.  ``None`` on any gap (vanished, zombie, malformed, timed out).
    """
    if pid < 1 or time.monotonic() >= deadline:
        return None
    if sys.platform == "linux":
        suffix = _proc_stat_suffix(pid)
        if suffix is None:
            return None
        if suffix[0] == "Z":
            return None
        if not suffix[19].isdigit():
            return None
        # A native birth read that finished at/after the shared stop deadline
        # is expired evidence, never a birth identity.
        if time.monotonic() >= deadline:
            return None
        return f"linux-start-ticks:{suffix[19]}"
    if sys.platform == "darwin":
        timeout = _bounded_probe_timeout(deadline)
        if timeout is None:
            return None
        try:
            completed = subprocess.run(
                ("ps", "-o", "lstart=", "-p", str(pid)),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        if completed.returncode != 0 or not value:
            return None
        # A native result that finished at/after the shared stop deadline is
        # expired evidence, never a birth identity.
        if time.monotonic() >= deadline:
            return None
        return f"ps-lstart:{value}"
    return None


@dataclass(frozen=True)
class BoundedBirthProof:
    """Tri-state bounded birth evidence for deadline-bound stop paths.

    ``status`` is ``"observed"`` (a native birth read finished inside the
    deadline; ``birth`` is its identity), ``"absent"`` (the native probe
    positively reported no such process), or ``"unknown"`` (expired
    deadline, timed-out probe, vanished mid-read, zombie, malformed, or
    otherwise inaccessible identity).  Unknown is never collapsed into
    absent.
    """

    status: str
    birth: str | None = None


def process_birth_proof(pid: int, deadline: float) -> BoundedBirthProof:
    """Bounded birth proof distinguishing observed birth, positive absence,
    and unknown gaps.

    Linux uses procfs only (a missing stat plus a failed null-signal on a
    usable procfs is positive absence); Darwin uses a single bounded ``ps``
    birth probe whose nonzero exit is positive absence and whose timeout is
    unknown.  Never falls back to the generic five-second probe, and a
    result that finishes at/after the deadline is expired evidence.
    """
    if pid < 1 or time.monotonic() >= deadline:
        return BoundedBirthProof("unknown")
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            if _linux_procfs_is_usable():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return BoundedBirthProof("absent")
                except (OSError, ValueError):
                    return BoundedBirthProof("unknown")
            return BoundedBirthProof("unknown")
        except (OSError, UnicodeError):
            return BoundedBirthProof("unknown")
        try:
            suffix = stat[stat.rfind(")") + 2 :].split()
            if suffix[0] == "Z":
                return BoundedBirthProof("absent")
            birth_ticks = suffix[19]
            if not birth_ticks.isdigit():
                return BoundedBirthProof("unknown")
        except IndexError:
            return BoundedBirthProof("unknown")
        if time.monotonic() >= deadline:
            return BoundedBirthProof("unknown")
        return BoundedBirthProof("observed", f"linux-start-ticks:{birth_ticks}")
    if sys.platform == "darwin":
        timeout = _bounded_probe_timeout(deadline)
        if timeout is None:
            return BoundedBirthProof("unknown")
        try:
            completed = subprocess.run(
                ("ps", "-o", "lstart=", "-p", str(pid)),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return BoundedBirthProof("unknown")
        if time.monotonic() >= deadline:
            return BoundedBirthProof("unknown")
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            return BoundedBirthProof("observed", f"ps-lstart:{value}")
        if completed.returncode != 0:
            return BoundedBirthProof("absent")
        return BoundedBirthProof("unknown")
    return BoundedBirthProof("unknown")


def controller_group_owned(
    pid: int, birth_expected: str, session_id: int | None, deadline: float
) -> bool:
    """Fresh bounded proof that ``pid`` still leads its own recorded group.

    Requires a live exact birth, an actual PGID equal to ``pid``, and, when
    ``session_id`` is given, that exact SID.  Uses bounded native checks only
    (procfs on Linux, a single bounded ``ps`` birth probe on Darwin); a
    missing, changed, reused, or unknown identity is never owned.
    """
    if time.monotonic() >= deadline:
        return False
    try:
        pgid = os.getpgid(pid)
    except (OSError, ValueError):
        return False
    if pgid != pid:
        return False
    if session_id is not None:
        try:
            if os.getsid(pid) != session_id:
                return False
        except (OSError, ValueError):
            return False
    if time.monotonic() >= deadline:
        return False
    observed = process_birth_bounded(pid, deadline)
    if observed is None or time.monotonic() >= deadline:
        return False
    return observed == birth_expected


def process_liveness(pid: int, deadline: float | None = None) -> ProcessLiveness:
    """Return only liveness evidence, keeping identity-observation gaps unknown.

    A missing birth identity is not enough to release an owner reservation: the
    process may still exist while procfs or ``ps`` cannot expose its birth
    data.  This separate probe therefore reports confirmed absence only when
    the operating system positively says that the PID is gone (or the process
    is a zombie); all observation failures remain ``unknown``.

    With a shared stop ``deadline``, the probe is bounded: Linux stays on
    procfs plus the native ``kill(0)`` probe and Darwin uses a single ``ps``
    probe whose timeout is the smaller of the two-second observation window
    and the remaining deadline; an expired deadline reports ``unknown``
    without any further work.  Without a deadline the generic contract (and
    its five-second fallback) is unchanged.
    """
    if pid < 1:
        return "absent"
    if deadline is not None and time.monotonic() >= deadline:
        return "unknown"

    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            closing = stat.rfind(")")
            suffix = stat[closing + 2 :].split()
            if closing < 0 or stat[closing + 1 : closing + 2] != " " or not suffix:
                raise ValueError("malformed process stat")
            # A native liveness read that finished at/after the shared stop
            # deadline is expired evidence, never presence or absence proof.
            if deadline is not None and time.monotonic() >= deadline:
                return "unknown"
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
                    if deadline is not None:
                        return "unknown"
                else:
                    return "present"
            elif deadline is not None:
                return "unknown"
        except (OSError, IndexError, UnicodeError, ValueError):
            if deadline is not None:
                return "unknown"

    if deadline is not None:
        timeout = _bounded_probe_timeout(deadline)
        if timeout is None:
            return "unknown"
    else:
        timeout = 5
    is_darwin = sys.platform == "darwin"
    try:
        completed = subprocess.run(
            (
                "ps",
                "-o",
                "pid=,stat=" if is_darwin else "pid=",
                "-p",
                str(pid),
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if deadline is not None and time.monotonic() >= deadline:
        # A successful native liveness result that finished at/after the
        # shared stop deadline is expired evidence, never absence proof.
        return "unknown"
    if is_darwin:
        if completed.returncode == 0 and completed.stdout.strip():
            fields = completed.stdout.strip().split()
            # The row must name the exact probed PID and carry its native
            # state field; a bare PID row is ambiguous, never present.
            if (
                len(fields) != 2
                or not fields[0].isdigit()
                or int(fields[0]) != pid
                or not fields[1]
            ):
                return "unknown"
            # A zombie is positively ceased, not live work.
            return "absent" if fields[1].startswith("Z") else "present"
        if completed.returncode != 0 and not completed.stdout.strip() and not completed.stderr.strip():
            return "absent"
        return "unknown"
    if completed.returncode == 0 and completed.stdout.strip():
        return "present"
    if completed.returncode != 0 and not completed.stdout.strip() and not completed.stderr.strip():
        return "absent"
    return "unknown"


# ---------------------------------------------------------------------------
# Session topology helpers (exclusive execution lifecycle)
# ---------------------------------------------------------------------------


def _proc_stat_suffix(pid: int) -> list[str] | None:
    """Return the field list after the comm field, or ``None`` on any gap.

    ``None`` covers both races (the process exited while reading) and
    genuine ambiguity; callers that need to distinguish a confirmed exit
    from an ambiguous observation must check the directory entry.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, IndexError):
        return None
    closing = raw.rfind(")")
    if closing < 0 or raw[closing + 1 : closing + 2] != " ":
        return None
    suffix = raw[closing + 2 :].split()
    if len(suffix) < 20:
        return None
    return suffix


def _ps_lstart_batch(pids: list[int], deadline: float) -> dict[int, str] | None:
    """Batched macOS birth identities; ``None`` on any ambiguity.

    The single batch probe is bounded by the shared stop ``deadline`` (no
    minimum floor), and a result that finished at/after the deadline is
    expired evidence, never identity proof.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        completed = subprocess.run(
            (
                "ps",
                "-o",
                "pid=,lstart=",
                "-p",
                ",".join(str(pid) for pid in pids),
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=remaining,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    if time.monotonic() >= deadline:
        # A batched birth result that finished at/after the shared stop
        # deadline is expired evidence, never identity proof.
        return None
    births: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        # Split on whitespace limited to one split so the complete lstart
        # string (which itself contains spaces) is preserved.  Leading
        # whitespace from right-aligned PID columns is stripped first; a
        # partition-before-strip would reject normal padded output.
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            return None
        pid_text, start_text = fields
        if not pid_text.isdigit():
            return None
        start = start_text.strip()
        if not start:
            return None
        births[int(pid_text)] = f"ps-lstart:{start}"
    return births


def _linux_session_members(
    session_id: int,
    deadline: float,
    max_members: int,
    max_bytes: int,
) -> list[SessionMember] | None:
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    members: list[SessionMember] = []
    scanned = 0
    bytes_read = 0
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        scanned += 1
        if scanned > max_members:
            return None
        if time.monotonic() >= deadline:
            return None
        pid = int(name)
        suffix = _proc_stat_suffix(pid)
        if suffix is None:
            # Distinguish a race (the entry is gone) from a genuinely
            # ambiguous procfs view, which must fail the whole observation.
            if not entry.exists():
                continue
            return None
        bytes_read += sum(len(part) for part in suffix)
        if bytes_read > max_bytes:
            return None
        try:
            pgid = int(suffix[2])
            sid = int(suffix[3])
        except ValueError:
            return None
        if sid != session_id:
            continue
        if suffix[0] == "Z":
            birth = None
        elif suffix[19].isdigit():
            birth = f"linux-start-ticks:{suffix[19]}"
        else:
            return None
        members.append(SessionMember(pid=pid, birth=birth, pgid=pgid))
        if len(members) > max_members:
            return None
    # An inventory whose final read finished at/after the shared stop
    # deadline is expired evidence, never a complete observation.
    if time.monotonic() >= deadline:
        return None
    return members


def _darwin_session_members(
    session_id: int,
    deadline: float,
    max_members: int,
    max_bytes: int,
) -> list[SessionMember] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        # Native Apple ps exposes no numeric session id (there is no ``sid``
        # keyword; the ``sess`` field is not a session-leader PID and is never
        # used as one).  Inventory PIDs, groups, and per-process state, then
        # resolve each candidate's numeric session through os.getsid.
        listing = subprocess.run(
            ("ps", "-axo", "pid=,pgid=,stat="),
            check=False,
            capture_output=True,
            text=True,
            timeout=remaining,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    if len(listing.stdout.encode("utf-8", "replace")) > max_bytes:
        return None
    listed: list[tuple[int, int]] = []
    zombie_pids: set[int] = set()
    for line in listing.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3:
            return None
        try:
            pid, pgid = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        state = parts[2]
        if not state:
            return None
        # A zombie is inventory-visible but not live work: it is captured
        # with no birth so it can never anchor, revalidate, or be signalled.
        if state[0] == "Z":
            zombie_pids.add(pid)
        listed.append((pid, pgid))
        if len(listed) > max_members:
            return None
    candidates: list[tuple[int, int]] = []
    for pid, pgid in listed:
        try:
            sid = os.getsid(pid)
        except ProcessLookupError:
            # Vanished during observation: independent positive absence, so it
            # may be omitted (later liveness revalidation confirms absence).
            continue
        except (OSError, ValueError):
            # Inaccessible/ambiguous: fail the whole observation, never emit a
            # partial or empty success.
            return None
        if sid == session_id:
            candidates.append((pid, pgid))
    if not candidates:
        return []
    if deadline - time.monotonic() <= 0:
        return None
    # ``_ps_lstart_batch`` takes the absolute shared stop deadline, not a
    # relative remaining budget.
    births = _ps_lstart_batch([pid for pid, _ in candidates], deadline)
    if births is None:
        return None
    members: list[SessionMember] = []
    for pid, pgid in candidates:
        if time.monotonic() >= deadline:
            return None
        try:
            # Revalidate the listing through direct syscalls so a process
            # that exited between the listing and the birth batch cannot be
            # captured with a stale or reused identity.
            if os.getsid(pid) != session_id or os.getpgid(pid) != pgid:
                continue
        except ProcessLookupError:
            continue
        except (OSError, ValueError):
            return None
        if pid in zombie_pids:
            birth = None
        else:
            birth = births.get(pid)
            if birth is None:
                return None
        members.append(SessionMember(pid=pid, birth=birth, pgid=pgid))
    return members


def session_members(
    session_id: int,
    *,
    deadline_seconds: float = SESSION_OBSERVATION_SECONDS,
    max_members: int = SESSION_MAX_MEMBERS,
    max_bytes: int = SESSION_MAX_BYTES,
) -> list[SessionMember] | None:
    """Observe one session's members in a single bounded pass.

    Returns ``None`` when the observation is incomplete, ambiguous, exceeds
    a bound, or the platform is unsupported.  An empty list is a positive
    result: the session is known to have no (observable) members.  This
    function never signals anything.
    """
    if session_id < 1:
        return None
    # The operation budget is the raw requested window (no minimum floor):
    # a floor would let one observation spend time past its shared stop
    # deadline when less than 50 ms remain.
    deadline = time.monotonic() + deadline_seconds
    if sys.platform == "linux":
        if not _linux_procfs_is_usable():
            return None
        return _linux_session_members(session_id, deadline, max_members, max_bytes)
    if sys.platform == "darwin":
        return _darwin_session_members(session_id, deadline, max_members, max_bytes)
    return None


def session_member_live(
    member: SessionMember, session_id: int, deadline: float | None = None
) -> bool:
    """Revalidate one captured member before it may be treated as live.

    A member is live only when its birth identity, session id, and process
    group all still match the captured values.  Reused PIDs, zombies, and
    unproven identities are never live.

    With a shared stop ``deadline``, the birth revalidation is bounded to the
    remaining window (native procfs on Linux, a single bounded ``ps`` probe
    on Darwin) instead of the generic five-second fallback; an expired
    deadline is never live.
    """
    if member.birth is None:
        return False
    if deadline is not None and time.monotonic() >= deadline:
        return False
    try:
        if os.getsid(member.pid) != session_id:
            return False
        if os.getpgid(member.pid) != member.pgid:
            return False
    except (OSError, ValueError):
        return False
    if deadline is None:
        return process_birth_identity(member.pid) == member.birth
    return process_birth_bounded(member.pid, deadline) == member.birth


def _darwin_group_state(pgid: int, deadline: float) -> GroupState:
    """Positive Darwin process-group state from a bounded native scan.

    A single bounded ``ps -axo pid=,pgid=,stat=`` scan distinguishes live
    work from zombies: the first live (non-zombie) member of the group makes
    it ``present``; a complete inventory with no members, or only zombie
    members, is positively ``absent`` (a zombie is not live work and an
    unreaped zombie keeps the numeric group alive to ``kill(-pgid, 0)``).
    Any malformed, over-bounded, failed, timed-out, or deadline-expired
    observation is ``unknown``.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "unknown"
    try:
        listing = subprocess.run(
            ("ps", "-axo", "pid=,pgid=,stat="),
            check=False,
            capture_output=True,
            text=True,
            timeout=remaining,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if listing.returncode != 0:
        return "unknown"
    if len(listing.stdout.encode("utf-8", "replace")) > SESSION_MAX_BYTES:
        return "unknown"
    seen = 0
    for line in listing.stdout.splitlines():
        if not line.strip():
            continue
        seen += 1
        if seen > SESSION_MAX_MEMBERS:
            return "unknown"
        parts = line.split()
        if len(parts) != 3:
            return "unknown"
        try:
            member_pgid = int(parts[1])
        except ValueError:
            return "unknown"
        state = parts[2]
        if not state:
            return "unknown"
        if member_pgid == pgid and state[0] != "Z":
            # A scan that finished at/after the shared deadline is expired
            # evidence, never positive presence.
            if time.monotonic() >= deadline:
                return "unknown"
            return "present"
    if time.monotonic() >= deadline:
        return "unknown"
    return "absent"


def process_group_state(pgid: int | None, deadline: float | None = None) -> GroupState:
    """Report a positive group state for termination evidence.

    Linux scans ``/proc`` once for a live (non-zombie) member of the group.
    macOS scans the native ``ps`` inventory for the same distinction: a
    zombie-only group is positively absent, a live member is present.
    Returns ``unknown`` on any ambiguity.

    With a shared stop ``deadline``, the Linux scan checks the deadline
    before and during the scan and the Darwin scan is bounded to the
    remaining window; an expired deadline reports ``unknown`` without
    further work.
    """
    if pgid is None or pgid < 1:
        return "unknown"
    if deadline is not None and time.monotonic() >= deadline:
        return "unknown"
    if sys.platform == "linux":
        try:
            entries = list(Path("/proc").iterdir())
        except OSError:
            return "unknown"
        for entry in entries:
            if deadline is not None and time.monotonic() >= deadline:
                return "unknown"
            name = entry.name
            if not name.isdigit():
                continue
            pid = int(name)
            suffix = _proc_stat_suffix(pid)
            if suffix is None:
                if not entry.exists():
                    continue
                return "unknown"
            try:
                member_pgid = int(suffix[2])
            except ValueError:
                return "unknown"
            if member_pgid == pgid and suffix[0] != "Z":
                if deadline is not None and time.monotonic() >= deadline:
                    return "unknown"
                return "present"
        # A scan whose final read finished at/after the shared stop deadline
        # is expired evidence, never positive absence.
        if deadline is not None and time.monotonic() >= deadline:
            return "unknown"
        return "absent"
    if sys.platform == "darwin":
        if deadline is None:
            deadline = time.monotonic() + SESSION_OBSERVATION_SECONDS
        return _darwin_group_state(pgid, deadline)
    return "unknown"


def _owned_group_still_owned(proc: subprocess.Popen[bytes], pgid: int) -> bool:
    if proc.poll() is not None:
        return False
    try:
        return os.getpgid(proc.pid) == pgid
    except (OSError, ValueError):
        return False


def _group_absent(pgid: int | None, deadline: float) -> bool:
    """Positive group absence inside an existing absolute kill-phase deadline.

    Every scan is bounded by the shared deadline (never a renewed window),
    and a scan that finished at/after the deadline is expired evidence: the
    probe reports False so the caller keeps ownership unconfirmed.
    """
    if pgid is None or pgid < 1:
        return False
    while time.monotonic() < deadline and process_group_state(pgid, deadline) == "present":
        time.sleep(0.05)
    if time.monotonic() >= deadline:
        return False
    return process_group_state(pgid, deadline) == "absent"


def _signal_owned_group(pgid: int, sig: signal.Signals) -> None:
    """Send ``sig`` to ``pgid``; a vanished group is not an error."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _group_positively_absent(
    pgid: int | None, deadline: float | None = None
) -> bool:
    """Positive absence of a group; an unknown state is not absence.

    With a shared phase ``deadline`` the native scan is bounded to it and a
    scan finishing at/after the deadline is expired evidence, never positive
    absence.
    """
    if pgid is None or pgid < 1:
        return True
    return process_group_state(pgid, deadline) == "absent"


def _capture_owned_group(
    proc: subprocess.Popen[bytes], pgid: int
) -> tuple[int, list[SessionMember]] | None:
    """Capture birth-validated members of the owned group before signalling.

    Returns ``(session_id, members)`` where ``members`` are the session
    members whose group is ``pgid``, or ``None`` when the observation is
    incomplete, ambiguous, or exceeds a bound.  Never signals anything.
    """
    try:
        session_id = os.getsid(proc.pid)
    except (OSError, ValueError):
        return None
    if session_id < 1:
        return None
    members = session_members(session_id, deadline_seconds=SESSION_OBSERVATION_SECONDS)
    if members is None:
        return None
    return session_id, [member for member in members if member.pgid == pgid]


def _owned_group_anchor_live(
    proc: subprocess.Popen[bytes],
    pgid: int | None,
    capture: tuple[int, list[SessionMember]] | None,
    deadline: float | None = None,
) -> bool:
    """Revalidate ownership of ``pgid`` immediately before a signal.

    True when the direct child still leads the group, or when a captured,
    birth/PGID/SID-validated member still owns the group.  A wrapper's exit
    alone does not disable this: a positively identified survivor keeps the
    group owned.  A reused/missing identity never proves ownership.
    """
    if pgid is None or pgid < 1:
        return False
    if _owned_group_still_owned(proc, pgid):
        return True
    if capture is None:
        return False
    session_id, members = capture
    return any(
        member.pgid == pgid and session_member_live(member, session_id, deadline)
        for member in members
    )


def terminate_owned_group(
    proc: subprocess.Popen[bytes],
    pgid: int | None,
    *,
    grace_seconds: float = 5.0,
    kill_seconds: float = 2.0,
) -> bool:
    """Bounded TERM/KILL escalation of one verified owned group.

    Captures the child's group/session and the birth identities of eligible
    live group members before signalling, and revalidates ownership after the
    capture window so the capture can never separate the ownership proof from
    the first TERM.  The monotonic grace deadline starts with the first
    TERM; the direct child is reaped independently while captured survivors
    are observed until that deadline, so a wrapper's exit alone never
    expires the provider's grace.  Teardown returns promptly once the child
    and the group are positively ceased.  At grace expiry a surviving group
    is escalated to KILL only while a captured,
    birth/PGID/SID-validated member still owns it, revalidated immediately
    before the bounded KILL, so a wrapper's exit does not disable escalation
    of a positively identified survivor and a reused/missing identity never
    proves ownership.  Returns ``True`` only after positive child and group
    cessation; an unproven or reused group retains ``False`` so the caller
    keeps the claim as unconfirmed.  Never raises.
    """
    capture: tuple[int, list[SessionMember]] | None = None
    grace_deadline: float | None = None
    if pgid is not None and pgid >= 1 and _owned_group_still_owned(proc, pgid):
        capture = _capture_owned_group(proc, pgid)
        # Ownership is revalidated after the (bounded, potentially slow)
        # capture and immediately before the first TERM.
        if _owned_group_still_owned(proc, pgid):
            _signal_owned_group(pgid, signal.SIGTERM)
            grace_deadline = time.monotonic() + max(0.0, grace_seconds)
    try:
        proc.terminate()
    except OSError:
        pass
    if grace_deadline is None:
        grace_deadline = time.monotonic() + max(0.0, grace_seconds)
    # Reap the direct child independently and observe captured survivors
    # until the group's grace deadline; the wrapper's exit alone never
    # expires the provider's grace.  Each group-cessation probe is bounded
    # to the remaining grace window, and positive completion is rechecked
    # against it.
    while time.monotonic() < grace_deadline:
        if proc.poll() is not None and _group_positively_absent(
            pgid,
            min(grace_deadline, time.monotonic() + SESSION_OBSERVATION_SECONDS),
        ):
            if time.monotonic() < grace_deadline:
                return True
        time.sleep(0.05)
    # At grace expiry ONE kill-phase deadline owns the rest of the teardown:
    # ownership proof, group KILL, direct-child KILL/wait and final group
    # cessation all use its remaining time (at most the two-second
    # observation window) without another renewed kill allowance.  Expired
    # evidence authorizes no signal and no positive cessation, so the
    # caller keeps the claim as unconfirmed.
    kill_deadline = time.monotonic() + min(
        SESSION_OBSERVATION_SECONDS, max(0.0, kill_seconds)
    )
    if _owned_group_anchor_live(proc, pgid, capture, kill_deadline):
        # Recheck immediately before the signal: expired proof authorizes
        # no KILL.
        if time.monotonic() < kill_deadline:
            _signal_owned_group(pgid, signal.SIGKILL)
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(0.0, kill_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
    if time.monotonic() >= kill_deadline:
        return False
    return _group_absent(pgid, kill_deadline)
