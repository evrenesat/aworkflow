"""Repository registry for managing known repos."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import RepoInfo


class RepoRegistryError(Exception):
    """Error in repo registry operations."""
    pass


class RepoRegistry:
    """Manages a registry of known git repositories."""

    def __init__(self, registry_path: Path) -> None:
        self._registry_path = registry_path
        self._repos: dict[str, RepoInfo] = {}
        self._load()

    def _load(self) -> None:
        """Load registry from disk."""
        if self._registry_path.exists():
            with open(self._registry_path, "r") as f:
                data = json.load(f)
            for repo_data in data.get("repos", []):
                repo = RepoInfo(
                    id=repo_data["id"],
                    name=repo_data["name"],
                    path=Path(repo_data["path"]),
                    is_git_root=repo_data.get("is_git_root", False),
                    registered_at=datetime.fromisoformat(repo_data["registered_at"]),
                )
                self._repos[repo.id] = repo

    def _save(self) -> None:
        """Save registry to disk."""
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "repos": [repo.to_dict() for repo in self._repos.values()],
            "version": 1,
        }
        with open(self._registry_path, "w") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _is_git_root(path: Path) -> bool:
        """Check if path is a git repository root."""
        git_dir = path / ".git"
        return git_dir.exists()

    def list_repos(self) -> list[RepoInfo]:
        """List all registered repositories."""
        return list(self._repos.values())

    def get_repo(self, project_id: str) -> RepoInfo | None:
        """Get a repository by ID."""
        return self._repos.get(project_id)

    def add_repo(self, path: str, name: str | None = None) -> RepoInfo:
        """Add a repository to the registry.

        Args:
            path: Path to the repository root.
            name: Optional name for the repo. Defaults to directory name.

        Returns:
            The newly registered RepoInfo.

        Raises:
            RepoRegistryError: If the path doesn't exist or is not a git root.
        """
        project_path = Path(path).expanduser().resolve()

        if not project_path.exists():
            raise RepoRegistryError(f"Path does not exist: {project_path}")

        if not project_path.is_dir():
            raise RepoRegistryError(f"Path is not a directory: {project_path}")

        is_git = self._is_git_root(project_path)

        # Check for existing repo with same path
        for existing in self._repos.values():
            if existing.path == project_path:
                raise RepoRegistryError(f"Repository already registered with id: {existing.id}")

        project_id = str(uuid4())[:8]
        repo_name = name or project_path.name

        repo = RepoInfo(
            id=project_id,
            name=repo_name,
            path=project_path,
            is_git_root=is_git,
            registered_at=datetime.now(timezone.utc),
        )

        self._repos[project_id] = repo
        self._save()

        return repo

    def remove_repo(self, project_id: str) -> bool:
        """Remove a repository from the registry.

        Args:
            project_id: ID of the project to remove.

        Returns:
            True if the repo was removed, False if it didn't exist.
        """
        if project_id in self._repos:
            del self._repos[project_id]
            self._save()
            return True
        return False

    def update_repo(self, project_id: str, name: str | None = None) -> RepoInfo | None:
        """Update a repository's metadata.

        Args:
            project_id: ID of the project to update.
            name: New name for the repo.

        Returns:
            Updated RepoInfo or None if repo doesn't exist.
        """
        repo = self._repos.get(project_id)
        if repo is None:
            return None

        if name is not None:
            repo = RepoInfo(
                id=repo.id,
                name=name,
                path=repo.path,
                is_git_root=repo.is_git_root,
                registered_at=repo.registered_at,
            )
            self._repos[project_id] = repo
            self._save()

        return repo
