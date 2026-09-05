"""Explicit, read-only relocation of current-schema resume metadata."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import copy
import re
import subprocess
from typing import Any, Mapping

from .plan import parse_git_tracking_metadata


def _absolute_path(value: object, field: str, *, existing: bool = False) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f"resume relocation: {field} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"resume relocation: {field} must be an absolute path without traversal")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"resume relocation: {field} contains a symlink")
    try:
        return path.resolve(strict=existing)
    except OSError as exc:
        raise ValueError(f"resume relocation: {field} is unavailable") from exc


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("resume relocation: Git identity could not be verified") from exc
    if result.returncode:
        raise ValueError(f"resume relocation: Git verification failed ({args[0]})")
    return result.stdout.strip()


@dataclass(frozen=True)
class ResumeRelocation:
    source_run_id: str
    source_repo_root: Path
    source_worktree_root: Path
    current_repo_root: Path
    current_worktree_root: Path

    def map_path(self, value: str | Path, *, required: bool = True) -> Path:
        path = _absolute_path(value, "recorded path")
        matches: list[Path] = []
        for source, target in (
            (self.source_repo_root, self.current_repo_root),
            (self.source_worktree_root, self.current_worktree_root),
        ):
            try:
                relative = path.relative_to(source)
            except ValueError:
                continue
            matches.append(_absolute_path(target / relative, "replacement path"))
        if len(matches) != 1:
            if not matches and not required:
                return path
            raise ValueError("resume relocation: recorded path is outside or ambiguous between source roots")
        return matches[0]

    def map_frozen_config_path(self, value: str | Path) -> Path:
        """Map only a source-root config path; leave external identities unchanged."""
        return self.map_path(value, required=False)

    def provenance(self) -> dict[str, object]:
        return {
            "source_run_id": self.source_run_id,
            "source_repo_root": str(self.source_repo_root),
            "source_worktree_root": str(self.source_worktree_root),
            "current_repo_root": str(self.current_repo_root),
            "current_worktree_root": str(self.current_worktree_root),
            "native_sessions_reopened": True,
        }

    def payload(self, source: Mapping[str, Any]) -> dict[str, Any]:
        """Remap schema path fields, never prose, selectors, hashes or scope IDs."""
        result = copy.deepcopy(dict(source))
        required = ("repo_root", "worktree_path", "plan_path", "original_plan_path", "active_plan_path")
        for field in required:
            value = result.get(field)
            if value is not None:
                result[field] = str(self.map_path(value))
        path_fields = {
            "run_dir", "new_plan_path", "last_manager_report_path", "artifact_path",
            "review_stdout_artifact_path", "repair_plan_path", "original_plan_path",
            "active_plan_path", "post_transition_active_plan_path",
            "override_source_run_dir", "scope_envelope_source_path",
        }
        identity_fields = {
            "checkpoint_identity", "target_plan_identity", "post_transition_checkpoint_identity",
        }
        def transform(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    transform(item)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if key in path_fields and isinstance(item, str) and Path(item).is_absolute():
                        value[key] = str(self.map_path(item, required=False))
                    elif key in identity_fields and isinstance(item, str) and "::checkpoint-" in item:
                        path, suffix = item.rsplit("::", 1)
                        value[key] = f"{self.map_path(path)}::{suffix}"
                    elif isinstance(item, (dict, list)):
                        transform(item)
        # Only known durable state subtrees; frozen configuration remains byte-equivalent.
        for field in (
            "manager_history", "review_rejection_history", "active_implementation_scope",
            "pending_manager_notes", "pending_step_team_override", "pending_boundary_decision",
            "pending_repartition", "repartition_history",
        ):
            transform(result.get(field))
        for field in (
            "run_dir",
            "new_plan_path",
            "last_manager_report_path",
            "override_source_run_dir",
            "scope_envelope_source_path",
        ):
            value = result.get(field)
            if isinstance(value, str) and Path(value).is_absolute():
                result[field] = str(self.map_path(value, required=False))
        result["active_role_sessions"] = []
        return result


def prepare_resume_relocation(
    source: Mapping[str, Any], *, source_run_id: str,
    current_repo_root: Path, replacement_worktree: str | Path,
) -> ResumeRelocation:
    if "worktree" not in source.get("lifecycle_setup", ()):
        raise ValueError("resume relocation requires a recorded linked-worktree lifecycle")
    for field in ("current_hotplug_transaction", "pending_hotplug_transaction"):
        if source.get(field) is not None:
            raise ValueError(f"resume relocation blocked by {field}")
    old_repo = _absolute_path(source.get("repo_root"), "source repo root")
    old_worktree = _absolute_path(source.get("worktree_path"), "source worktree root")
    repo = _absolute_path(current_repo_root, "current repo root", existing=True)
    worktree = _absolute_path(replacement_worktree, "replacement worktree", existing=True)
    for first, second in ((old_repo, old_worktree), (repo, worktree)):
        if first == second or first in second.parents or second in first.parents:
            raise ValueError("resume relocation requires distinct non-overlapping repository and worktree roots")
    feature, main = source.get("feature_branch"), source.get("main_branch")
    if not all(isinstance(value, str) and value for value in (feature, main)):
        raise ValueError("resume relocation requires recorded main and feature branches")
    if _git(repo, "symbolic-ref", "--short", "HEAD") != main:
        raise ValueError("resume relocation requires the primary checkout on the recorded main branch")
    for branch in (main, feature):
        _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
    records = _git(repo, "worktree", "list", "--porcelain").split("\n\n")
    found = False
    for record in records:
        lines = record.splitlines()
        if f"worktree {worktree}" in lines and f"branch refs/heads/{feature}" in lines:
            found = True
    if not found or _git(worktree, "symbolic-ref", "--short", "HEAD") != feature:
        raise ValueError("resume relocation requires the exact registered worktree on the recorded feature branch")
    if Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree:
        raise ValueError("resume relocation requires the exact worktree root")
    for root in (repo, worktree):
        gitdir = Path(_git(root, "rev-parse", "--absolute-git-dir"))
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
            if (gitdir / marker).exists():
                raise ValueError(f"resume relocation blocked by in-progress Git operation {marker}")
    relocation = ResumeRelocation(source_run_id, old_repo, old_worktree, repo, worktree)
    plan = relocation.map_path(source.get("original_plan_path"))
    parsed = parse_git_tracking_metadata(plan.read_text(encoding="utf-8"))
    base = parsed.pre_handoff_base_head if parsed is not None else None
    if not isinstance(base, str) or re.fullmatch(r"[0-9a-fA-F]{7,40}", base) is None:
        raise ValueError("resume relocation requires a recorded Pre-Handoff Base HEAD commit")
    _git(repo, "cat-file", "-e", f"{base}^{{commit}}")
    return relocation
