"""Install and execute the external playbooks for the bundled NO-OP plan."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import re
import shlex
import subprocess

DEFAULT_STATE_DIR = Path("/tmp/aflow_noop_plan")
WORKER_PLAYBOOK = """# Actions: cleanup; sleep N; done; fail MESSAGE; incomplete MESSAGE
# Missing CP entries mean done. Semicolons separate actions; # starts a comment line.
# Example: CP1: cleanup; done
# Example: CP2: sleep 2; done
# Example: CP3: fail BLAH BLAH
# Example: CP1: incomplete MOCK_CAPABILITY_FAILURE needs a stronger worker
"""
REVIEWER_PLAYBOOK = """# Actions: sleep N; pass; fail MESSAGE; reject MESSAGE
# Missing CP entries mean pass (no sleep).
# Example: CP1: sleep 60; pass
# Example: CP2: sleep 0; pass
# Example: CP3: fail BOOO
# Example: CP3: reject Retry this checkpoint after I edit the playbook
"""
ENTRY = re.compile(r"CP([1-9][0-9]*)\s*:\s*(.+)", re.IGNORECASE)
MARKER = re.compile(r"CP_[1-9][0-9]*\.txt")


class NoopError(ValueError):
    """Invalid fixture setup or playbook; never silently pass it."""


def _plain_path(path: Path, *, directory: bool = False) -> None:
    # Check parents too: cleanup must not follow a redirected fixture tree.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise NoopError(f"refusing symlink path: {path}")
    if path.exists() and (not path.is_dir() if directory else not path.is_file()):
        raise NoopError(f"expected {'directory' if directory else 'file'}: {path}")


def _state_paths(state_dir: Path) -> tuple[Path, Path, Path]:
    _plain_path(state_dir, directory=True)
    checkpoints = state_dir / "checkpoints"
    _plain_path(checkpoints, directory=True)
    worker, reviewer = (
        state_dir / f"{role}.playbook" for role in ("worker", "reviewer")
    )
    for path in (worker, reviewer):
        _plain_path(path)
    return checkpoints, worker, reviewer


def _markers(checkpoints: Path) -> list[Path]:
    paths = [p for p in checkpoints.glob("CP_*.txt") if MARKER.fullmatch(p.name)]
    for path in paths:
        _plain_path(path)
    return paths


def install_noop_plan(
    output: Path,
    *,
    state_dir: Path = DEFAULT_STATE_DIR,
    checkpoints: int = 3,
    force: bool = False,
    reset: bool = False,
) -> str:
    if not 1 <= checkpoints <= 100:
        raise NoopError("checkpoints must be between 1 and 100")
    output, state_dir = (
        output.expanduser().absolute(),
        state_dir.expanduser().absolute(),
    )
    _plain_path(output)
    marker_dir, worker, reviewer = _state_paths(state_dir)
    if output == state_dir or state_dir in output.parents:
        raise NoopError("the plan must be outside the external state directory")
    if output.exists() and not force:
        raise NoopError(f"{output} exists; use --force to replace only the plan")
    old_markers = _markers(marker_dir) if reset else []
    template = (
        files("aflow").joinpath("templates/noop-plan.md").read_text(encoding="utf-8")
    )
    sections = "\n\n".join(
        f"### [ ] Checkpoint {n}: Playbook CP{n}\n\n"
        f"- [ ] Read the worker playbook and run `aflow noop-step worker {n} "
        f"--state-dir {shlex.quote(str(state_dir))}`. On success verify "
        f"`checkpoints/CP_{n}.txt` beneath the external state directory, then check "
        "this step and heading. On failure leave them unchecked."
        for n in range(1, checkpoints + 1)
    )
    content = template.replace("{{STATE_DIR}}", str(state_dir)).replace(
        "{{CHECKPOINTS}}", sections
    )
    # Branch/worktree workflows replace this with their execution branch.
    # In-place workflows do not, so supply the known branch at generation.
    directory = output.parent
    while not directory.exists():
        directory = directory.parent
    try:
        branch = subprocess.run(
            ["git", "-C", str(directory), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if branch.returncode == 0 and branch.stdout.strip():
            content = content.replace(
                "- Plan Branch: ``", f"- Plan Branch: `{branch.stdout.strip()}`"
            )
    except (OSError, subprocess.SubprocessError):
        pass  # Creating the fixture outside a Git checkout is supported.
    marker_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    for path, text in ((worker, WORKER_PLAYBOOK), (reviewer, REVIEWER_PLAYBOOK)):
        if reset or not path.exists():
            path.write_text(text, encoding="utf-8")
    for path in old_markers:
        path.unlink()
    output.write_text(content, encoding="utf-8")
    return (
        f"Created {output} ({checkpoints} checkpoints).\n"
        f"Playbooks: {worker} and {reviewer}\n"
        "Defaults: workers touch CP_N.txt; reviewers pass immediately.\n"
        "Edit examples: CP1: cleanup; done | CP2: sleep 60; pass | CP3: fail BOOO\n"
        "Reviewer reject MESSAGE requests a normal repair loop; fail MESSAGE stops.\n"
        "Worker incomplete MESSAGE leaves work pending for review/manager escalation.\n"
        "Sleep invokes the system sleep command. Playbooks are re-read each call.\n"
        f"Run: aflow run --plan {shlex.quote(str(output))} --team TEAM --workflow WORKFLOW\n"
        "Use a disposable git repository: workflow setup/commits/merge still apply.\n"
        "Created outside Git? Initialize a branch and regenerate before an in-place run.\n"
        "Re-create with --force; add --reset to reset playbooks and CP markers.\n"
        "Existing playbooks/markers are otherwise preserved. Use a separate\n"
        "--state-dir for concurrent runs; the default directory is shared.\n"
    )


def _actions(text: str, role: str, checkpoint: int) -> list[tuple[str, str]]:
    entries: dict[int, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENTRY.fullmatch(line)
        if match is None:
            raise NoopError(f"playbook line {number}: expected CP<N>: ACTION")
        cp = int(match[1])
        if cp in entries:
            raise NoopError(f"duplicate CP{cp} entry")
        entries[cp] = match[2]
    default = "done" if role == "worker" else "pass"
    actions = []
    for part in entries.get(checkpoint, default).split(";"):
        bits = part.strip().split(maxsplit=1)
        if not bits:
            raise NoopError("empty action")
        action, argument = bits[0].lower(), bits[1] if len(bits) == 2 else ""
        if action == "sleep":
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", argument):
                raise NoopError("sleep requires non-negative seconds")
        elif action in {"fail", "reject", "incomplete"}:
            if (
                not argument
                or (action == "reject" and role != "reviewer")
                or (action == "incomplete" and role != "worker")
            ):
                raise NoopError(
                    "fail needs a message; reject is reviewer-only; incomplete is worker-only"
                )
        elif action in ({"cleanup", "done"} if role == "worker" else {"pass"}):
            if argument:
                raise NoopError(f"{action} takes no argument")
        else:
            raise NoopError(f"unsupported {role} action: {action}")
        actions.append((action, argument))
    for action, _ in actions[:-1]:
        if action in {"fail", "reject", "incomplete"}:
            raise NoopError("fail/reject/incomplete must be the last action")
    return actions


def execute_noop_step(
    role: str, checkpoint: int, *, state_dir: Path = DEFAULT_STATE_DIR
) -> tuple[int, str]:
    if role not in {"worker", "reviewer"} or checkpoint < 1:
        raise NoopError("expected worker/reviewer and a positive checkpoint number")
    state_dir = state_dir.expanduser().absolute()
    marker_dir, worker, reviewer = _state_paths(state_dir)
    playbook = worker if role == "worker" else reviewer
    if not playbook.is_file() or not marker_dir.is_dir():
        raise NoopError("fixture missing; run aflow noop-plan first")
    actions = _actions(playbook.read_text(encoding="utf-8"), role, checkpoint)
    marker = marker_dir / f"CP_{checkpoint}.txt"
    _plain_path(marker)
    cleanup = _markers(marker_dir) if any(a == "cleanup" for a, _ in actions) else []
    # A failed repeated worker attempt must not leave stale success evidence.
    if role == "worker":
        marker.unlink(missing_ok=True)
    for action, argument in actions:
        if action == "sleep":
            subprocess.run(["sleep", argument], check=True)
        elif action == "cleanup":
            for path in cleanup:
                path.unlink(missing_ok=True)
        elif action == "fail":
            return 1, f"AFLOW_STOP: AFLOW_NOOP_MOCK_FAILURE CP{checkpoint}: {argument}"
        elif action == "reject":
            return 2, f"AFLOW_NOOP_REJECT CP{checkpoint}: {argument}"
        elif action == "incomplete":
            return 3, f"AFLOW_NOOP_INCOMPLETE CP{checkpoint}: {argument}"
    if role == "worker":
        marker.touch()
    return 0, f"AFLOW_NOOP_OK {role} CP{checkpoint}"
