"""Explicit repository-local publication at the successful workflow boundary."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile


class PublicationError(RuntimeError):
    pass


def _git(root: Path, *args: str, optional: bool = False) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True,
        timeout=120, env=None,
    )
    if result.returncode and not (optional and result.returncode in (1, 128)):
        # Git output may contain credential-bearing URLs. Keep failures bounded.
        raise PublicationError(f"git {args[0]} failed; inspect the publication checkout")
    return result.stdout.strip()


def publish_completed_run(root: Path, run_dir: Path, *, source_ref: str = "HEAD") -> str | None:
    """Publish only after verified plan completion; never mutate the execution tree.

    Local Git configuration is an explicit owner grant, not tracked project code.
    Both aflow.publishRemote and aflow.publishBranch are required to opt in.
    """
    remote = _git(root, "config", "--local", "--get", "aflow.publishRemote", optional=True)
    branch = _git(root, "config", "--local", "--get", "aflow.publishBranch", optional=True)
    if not remote and not branch:
        return None
    receipt = {"status": "pending", "remote": remote, "branch": branch}
    path = run_dir / "publication.json"

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
            pending = Path(stream.name)
        pending.replace(path)

    worktree: Path | None = None
    try:
        if not remote or not branch or remote.startswith("-") or branch.startswith("-"):
            raise PublicationError("configure both aflow.publishRemote and aflow.publishBranch")
        if remote not in _git(root, "remote").splitlines():
            raise PublicationError("publication remote is not a configured Git remote")
        _git(root, "check-ref-format", f"refs/heads/{branch}")
        if _git(root, "status", "--porcelain", "--untracked-files=normal"):
            raise PublicationError("publication requires a clean completed execution checkout")
        candidate = _git(root, "rev-parse", f"{source_ref}^{{commit}}")
        receipt["source_commit"] = candidate
        save()
        _git(root, "fetch", remote, f"refs/heads/{branch}")
        published = _git(root, "rev-parse", "FETCH_HEAD^{commit}")
        base = _git(root, "merge-base", candidate, published)
        if base == candidate:
            receipt.update(status="published", commit=published)
            save()
            return published
        if base != published:
            # Reconcile accepted remote history outside every active checkout.
            worktree = Path(tempfile.mkdtemp(prefix="aflow-publication-")) / "checkout"
            receipt["checkout"] = str(worktree)
            save()
            _git(root, "worktree", "add", "--detach", str(worktree), candidate)
            _git(worktree, "merge", "--no-edit", published)
            candidate = _git(worktree, "rev-parse", "HEAD")
        # Normal fast-forward-only remote admission. Never force or overwrite main.
        _git(root, "push", remote, f"{candidate}:refs/heads/{branch}")
        receipt.update(status="published", commit=candidate)
        save()
        if worktree is not None:
            _git(root, "worktree", "remove", str(worktree))
        return candidate
    except (PublicationError, subprocess.TimeoutExpired, OSError) as exc:
        receipt.update(status="failed", error=type(exc).__name__)
        save()
        detail = f"; preserved merge checkout: {worktree}" if worktree else ""
        raise PublicationError(f"publication to {remote}/{branch} did not finish{detail}") from exc
