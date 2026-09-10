"""Resolve and load the current workflow configuration pair.

The live pair is the only configuration input this module loads.  A run may
retain a snapshot directory and an old fingerprint for diagnostics, but those
artifacts are never used as a fallback or as an integrity gate.

Source selection is deliberately separate from reading.  An explicitly
selected path and a persisted live path are authoritative and must exist.  A
legacy snapshot origin is only a best-effort path hint; when it is unavailable
the normal configured default is selected.  The selected ``aflow.toml`` and
its optional sibling are parsed once while holding the existing pair lock.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from .config import (
    ConfigError,
    WorkflowUserConfig,
    _config_path,
    load_workflow_config,
)
from .run_config_snapshot import (
    RunConfigSnapshot,
    SnapshotError,
    configuration_pair_lock,
    load_run_config_snapshot,
)


class LiveConfigError(ConfigError):
    """The selected live workflow configuration cannot be used."""


LiveConfigSourceKind = Literal[
    "explicit",
    "saved",
    "legacy_snapshot_origin",
    "default",
]


@dataclass(frozen=True)
class LiveConfigSource:
    """The selected current configuration pair and its provenance."""

    config_path: Path
    workflows_path: Path
    kind: LiveConfigSourceKind

    @property
    def source_path(self) -> Path:
        """Return the selected ``aflow.toml`` path."""
        return self.config_path

    @property
    def path(self) -> Path:
        """Compatibility alias for callers that treat a source as a path."""
        return self.config_path

    @property
    def source_kind(self) -> LiveConfigSourceKind:
        return self.kind

    @property
    def is_explicit(self) -> bool:
        return self.kind == "explicit"


@dataclass(frozen=True)
class LoadedLiveConfig:
    """One validated configuration object together with the source it used."""

    workflow_config: WorkflowUserConfig
    source: LiveConfigSource

    @property
    def config(self) -> WorkflowUserConfig:
        """Short alias for the parsed workflow configuration."""
        return self.workflow_config

    @property
    def config_path(self) -> Path:
        return self.source.config_path

    @property
    def workflows_path(self) -> Path:
        return self.source.workflows_path


def _resolve_relative_settings(
    workflow_config: WorkflowUserConfig,
    source: LiveConfigSource,
) -> WorkflowUserConfig:
    """Resolve configuration-defined filesystem paths from the live file."""
    worktree_root = getattr(workflow_config.aflow, "worktree_root", None)
    if (
        worktree_root is None
        or worktree_root.startswith("~")
        or Path(worktree_root).is_absolute()
    ):
        return workflow_config
    return replace(
        workflow_config,
        aflow=replace(
            workflow_config.aflow,
            worktree_root=str((source.config_path.parent / worktree_root).resolve()),
        ),
    )


def _path_value(
    value: str | Path,
    *,
    relative_to: Path | None = None,
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() and relative_to is not None:
        path = relative_to / path
    return path.resolve()


def _provided(value: object) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _is_regular_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _require_live_file(path: Path, *, source_kind: str) -> None:
    if _is_regular_file(path):
        return
    if path.exists():
        detail = "is not a regular file"
    else:
        detail = "does not exist"
    raise LiveConfigError(
        f"live workflow configuration {detail} for {source_kind} source: {path}"
    )


def _legacy_origin_path(
    value: str | Path | RunConfigSnapshot | Mapping[str, Any] | None,
) -> str | Path | None:
    if value is None:
        return None
    if isinstance(value, RunConfigSnapshot):
        return value.origin_config_path
    if isinstance(value, Mapping):
        origin = value.get("origin")
        if isinstance(origin, Mapping):
            value = origin.get("config_path")
        else:
            value = value.get("config_path")
        if value is None:
            return None
    if isinstance(value, (str, Path)):
        return value
    raise TypeError("legacy snapshot origin must be a path or snapshot metadata mapping")


def resolve_live_config_source(
    config_path: str | Path | None = None,
    *,
    explicit_config_path: str | Path | None = None,
    saved_live_config_path: str | Path | None = None,
    legacy_snapshot_origin: (
        str | Path | RunConfigSnapshot | Mapping[str, Any] | None
    ) = None,
    default_config_path: str | Path | None = None,
    legacy_origin_base_dir: str | Path | None = None,
) -> LiveConfigSource:
    """Choose the one current source for a configuration read.

    ``config_path`` and ``explicit_config_path`` are equivalent explicit
    inputs; the latter exists to make call sites self-documenting.  A missing
    explicit or saved path is an error.  A missing legacy origin is ignored as
    historical metadata and falls through to ``default_config_path`` (or the
    normal user default).
    """
    if config_path is not None and explicit_config_path is not None:
        raise TypeError("pass only one of config_path and explicit_config_path")
    selected_explicit = (
        explicit_config_path if explicit_config_path is not None else config_path
    )

    if selected_explicit is not None:
        selected = _path_value(selected_explicit)
        _require_live_file(selected, source_kind="explicit")
        return LiveConfigSource(selected, selected.with_name("workflows.toml"), "explicit")

    if _provided(saved_live_config_path):
        selected = _path_value(saved_live_config_path)  # type: ignore[arg-type]
        _require_live_file(selected, source_kind="saved")
        return LiveConfigSource(selected, selected.with_name("workflows.toml"), "saved")

    origin = _legacy_origin_path(legacy_snapshot_origin)
    if origin is not None:
        base_dir = (
            _path_value(legacy_origin_base_dir)
            if legacy_origin_base_dir is not None
            else None
        )
        selected = _path_value(origin, relative_to=base_dir)
        if _is_regular_file(selected):
            return LiveConfigSource(
                selected,
                selected.with_name("workflows.toml"),
                "legacy_snapshot_origin",
            )

    selected = _path_value(
        default_config_path if default_config_path is not None else _config_path()
    )
    _require_live_file(selected, source_kind="default")
    return LiveConfigSource(selected, selected.with_name("workflows.toml"), "default")


def load_live_config(
    config_path: str | Path | LiveConfigSource | None = None,
    *,
    source: LiveConfigSource | None = None,
    explicit_config_path: str | Path | None = None,
    saved_live_config_path: str | Path | None = None,
    legacy_snapshot_origin: (
        str | Path | RunConfigSnapshot | Mapping[str, Any] | None
    ) = None,
    default_config_path: str | Path | None = None,
    legacy_origin_base_dir: str | Path | None = None,
    loader: Callable[[Path], WorkflowUserConfig] | None = None,
) -> LoadedLiveConfig:
    """Load the selected current configuration pair under its pair lock.

    ``load_workflow_config`` intentionally keeps its historical empty-default
    behavior for general callers.  This helper checks the selected live file
    again inside the lock so an existing run can never turn a missing source
    into an empty configuration or silently use a copied snapshot.
    """
    if isinstance(config_path, LiveConfigSource):
        if source is not None:
            raise TypeError("pass only one source")
        source = config_path
        config_path = None
    elif source is not None and config_path is not None:
        raise TypeError("pass either config_path or source, not both")

    selected = source or resolve_live_config_source(
        config_path,
        explicit_config_path=explicit_config_path,
        saved_live_config_path=saved_live_config_path,
        legacy_snapshot_origin=legacy_snapshot_origin,
        default_config_path=default_config_path,
        legacy_origin_base_dir=legacy_origin_base_dir,
    )
    parse = loader or load_workflow_config
    with configuration_pair_lock(selected.config_path.parent):
        _require_live_file(selected.config_path, source_kind=selected.kind)
        try:
            workflow_config = parse(selected.config_path)
        except ConfigError:
            raise
        except OSError as exc:
            raise LiveConfigError(
                f"unable to read live workflow configuration {selected.config_path}: {exc}"
            ) from exc
    return LoadedLiveConfig(
        workflow_config=_resolve_relative_settings(workflow_config, selected),
        source=selected,
    )


def _metadata_live_config_path(metadata: Mapping[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    value = metadata.get("live_config_path")
    if value is None:
        frozen = metadata.get("frozen_config")
        if isinstance(frozen, Mapping):
            value = frozen.get("live_config_path")
    return value if isinstance(value, str) and value.strip() else None


def load_live_config_for_run(
    repo_root: str | Path,
    run_id: str,
    *,
    config_path: str | Path | None = None,
    saved_live_config_path: str | Path | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    default_config_path: str | Path | None = None,
    loader: Callable[[Path], WorkflowUserConfig] | None = None,
) -> LoadedLiveConfig:
    """Load current configuration for a run without trusting its snapshots.

    New metadata may provide ``live_config_path``.  For older metadata, the
    origin in ``snapshot.json`` is only a path hint.  Snapshot parse/hash/file
    failures are historical damage and therefore fall through to the normal
    current default; the selected live source itself is still strict.
    """
    saved_path = saved_live_config_path or _metadata_live_config_path(run_metadata)
    legacy_origin: str | None = None
    legacy_base: Path | None = None
    if not _provided(config_path) and not _provided(saved_path):
        try:
            snapshot = load_run_config_snapshot(Path(repo_root), run_id)
        except SnapshotError:
            snapshot = None
        if snapshot is not None:
            legacy_origin = snapshot.origin_config_path
            legacy_base = snapshot.directory

    return load_live_config(
        config_path,
        saved_live_config_path=saved_path,
        legacy_snapshot_origin=legacy_origin,
        legacy_origin_base_dir=legacy_base,
        default_config_path=default_config_path,
        loader=loader,
    )


# Keep the intended concept discoverable to callers that use the longer name.
load_current_workflow_config = load_live_config
resolve_config_source = resolve_live_config_source
