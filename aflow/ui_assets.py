"""Build and locate the bundled AFlow UI web assets.

Normal (wheel) installations ship prebuilt assets inside the ``aflow`` package
at ``aflow/ui_web/`` and never need Node/npm. Editable development installs
resolve the source checkout, run ``npm ci``/the TypeScript/Vite build when the
web inputs changed, and publish the completed asset directory atomically so a
failed rebuild never leaves stale or partial assets behind.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ASSETS_PACKAGE_SUBDIR = "ui_web"
_FINGERPRINT_NAME = ".build-fingerprint.json"
_LOCK_NAME = ".ui-assets.lock"
_BUILD_STAGE_PREFIX = ".ui-assets-stage-"
_FINGERPRINT_SCHEMA = 1
# Required developer toolchain versions (matches the CI toolchain).
REQUIRED_NODE_MAJOR = 22


class AssetBuildError(RuntimeError):
    """Raised when the UI web assets cannot be built or located."""


@dataclass(frozen=True)
class AssetBuildResult:
    out_dir: Path
    rebuilt: bool
    npm_install: bool
    fingerprint: str


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def published_assets_dir() -> Path | None:
    """Return the packaged asset directory next to the aflow package, if built."""
    candidate = _package_dir() / ASSETS_PACKAGE_SUBDIR
    if (candidate / "index.html").is_file():
        return candidate
    return None


def bundled_assets_available() -> bool:
    return published_assets_dir() is not None


def find_web_source_dir(start: Path | None = None) -> Path | None:
    """Locate the web subproject of the source checkout for editable installs.

    Never infers the checkout from the current working directory; it walks up
    from the installed aflow package itself.
    """
    current = (start or _package_dir()).resolve()
    for candidate in (current, *current.parents):
        marker = candidate / "apps" / "aflow_app" / "web" / "package.json"
        if marker.is_file():
            return marker.parent
        if candidate == candidate.parent:
            break
    return None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_source_fingerprint(web_dir: Path) -> str:
    """Hash every web build input: sources, configs, and the npm lockfile."""
    entries: list[tuple[str, str]] = []
    for path in sorted(web_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(web_dir).as_posix()
        if rel == "package-lock.json":
            entries.append((rel, _hash_file(path)))
            continue
        if (
            rel.startswith(("node_modules/", "dist/", "."))
            or fnmatch.fnmatch(rel, "src/*.test.*")
            or rel == "src/test-setup.ts"
        ):
            continue
        entries.append((rel, _hash_file(path)))
    if not any(rel == "package-lock.json" for rel, _ in entries):
        raise AssetBuildError(f"missing package-lock.json in {web_dir}")
    digest = hashlib.sha256()
    for rel, hash_value in sorted(entries):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hash_value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _fingerprint_path(out_dir: Path) -> Path:
    return out_dir / _FINGERPRINT_NAME


def read_cached_fingerprint(out_dir: Path) -> dict[str, object] | None:
    path = _fingerprint_path(out_dir)
    if not path.is_file() or not (out_dir / "index.html").is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _FINGERPRINT_SCHEMA:
        return None
    return payload


def _write_fingerprint(out_dir: Path, fingerprint: str, npm_install: bool) -> None:
    payload = {
        "schema": _FINGERPRINT_SCHEMA,
        "fingerprint": fingerprint,
        "npm_install": npm_install,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _fingerprint_path(out_dir).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _require_tool(name: str) -> str:
    discovered = shutil.which(name)
    if discovered is None:
        raise AssetBuildError(
            f"building the AFlow UI assets requires Node.js {REQUIRED_NODE_MAJOR} "
            f"and npm, but '{name}' was not found on PATH. Install Node.js "
            f"{REQUIRED_NODE_MAJOR} (for example from https://nodejs.org or your "
            "package manager) and retry; normal wheel installations never need it."
        )
    return discovered


def _run(command: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(
        command, cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AssetBuildError(
            f"command failed in {cwd}: {' '.join(command)}\n{detail[-4000:]}"
        )


def build_assets_into(
    web_dir: Path,
    out_dir: Path,
    *,
    progress=None,
) -> AssetBuildResult:
    """Run npm ci (when needed) and the Vite build into ``out_dir``."""
    report = progress or (lambda message: None)
    web_dir = web_dir.resolve()
    out_dir = Path(out_dir)
    _require_tool("node")
    _require_tool("npm")
    npm = shutil.which("npm") or "npm"
    fingerprint = compute_source_fingerprint(web_dir)
    node_modules = web_dir / "node_modules"
    npm_install = not node_modules.is_dir() or not (node_modules / ".package-lock.json").is_file()
    if npm_install:
        report("Installing web dependencies (npm ci)...")
        _run([npm, "ci", "--no-audit", "--no-fund"], cwd=web_dir)
    report("Building UI assets (tsc && vite build)...")
    _run(
        [npm, "run", "build", "--", "--outDir", str(out_dir), "--emptyOutDir"],
        cwd=web_dir,
    )
    if not (out_dir / "index.html").is_file():
        raise AssetBuildError(f"UI build did not produce index.html in {out_dir}")
    _write_fingerprint(out_dir, fingerprint, npm_install=npm_install)
    return AssetBuildResult(
        out_dir=out_dir, rebuilt=True, npm_install=npm_install, fingerprint=fingerprint
    )


def _publish(published: Path, staged: Path) -> None:
    """Replace ``published`` with ``staged`` as completely as POSIX allows."""
    retired = published.with_name(published.name + ".retired")
    if retired.exists():
        shutil.rmtree(retired, ignore_errors=True)
    if published.exists():
        os.replace(published, retired)
    try:
        os.replace(staged, published)
    except OSError:
        if retired.exists() and not published.exists():
            os.replace(retired, published)
        raise
    if retired.exists():
        shutil.rmtree(retired, ignore_errors=True)


def ensure_checkout_assets(*, force: bool = False, progress=None) -> AssetBuildResult:
    """Build the web assets for an editable install and publish them atomically.

    Skips npm/network work and rebuilding when the recorded fingerprint matches
    the published assets. Concurrent builds are serialized with a lock file and
    the completed asset directory is published only after a successful build.
    """
    web_dir = find_web_source_dir()
    if web_dir is None:
        raise AssetBuildError(
            "this aflow installation does not include the web UI sources; "
            "reinstall the aworkflow wheel to get prebuilt UI assets"
        )
    published = _package_dir() / ASSETS_PACKAGE_SUBDIR
    report = progress or (lambda message: None)
    if not force:
        cached = read_cached_fingerprint(published)
        if cached is not None:
            try:
                current = compute_source_fingerprint(web_dir)
            except AssetBuildError:
                current = None
            if current is not None and cached.get("fingerprint") == current:
                return AssetBuildResult(
                    out_dir=published,
                    rebuilt=False,
                    npm_install=False,
                    fingerprint=current,
                )
    lock_path = web_dir / _LOCK_NAME
    import fcntl

    with open(lock_path, "w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if not force:
                cached = read_cached_fingerprint(published)
                if cached is not None:
                    current = None
                    try:
                        current = compute_source_fingerprint(web_dir)
                    except AssetBuildError:
                        current = None
                    if current is not None and cached.get("fingerprint") == current:
                        return AssetBuildResult(
                            out_dir=published,
                            rebuilt=False,
                            npm_install=False,
                            fingerprint=current,
                        )
            stage = Path(tempfile.mkdtemp(prefix=_BUILD_STAGE_PREFIX, dir=str(web_dir)))
            try:
                result = build_assets_into(web_dir, stage, progress=report)
                _publish(published, stage)
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return AssetBuildResult(
        out_dir=published,
        rebuilt=True,
        npm_install=result.npm_install,
        fingerprint=result.fingerprint,
    )


def ensure_launch_assets(*, progress=None) -> Path:
    """Return a usable asset directory for ``aflow ui``, building when needed.

    Wheel installations only verify the bundled assets. Editable installations
    build/publish the checkout assets when they are missing or stale.
    """
    report = progress or (lambda message: None)
    published = published_assets_dir()
    if published is not None:
        web_dir = find_web_source_dir()
        if web_dir is None:
            # Wheel install: the bundled assets are authoritative.
            return published
        cached = read_cached_fingerprint(published)
        if cached is not None:
            try:
                if cached.get("fingerprint") == compute_source_fingerprint(web_dir):
                    return published
            except AssetBuildError:
                return published
    if find_web_source_dir() is None:
        if published is not None:
            return published
        raise AssetBuildError(
            "the AFlow UI assets are missing from this installation; reinstall "
            "the aworkflow wheel (normal installations bundle them, so this "
            "indicates a broken install)"
        )
    report("UI assets are missing or stale; building them now...")
    return ensure_checkout_assets(progress=report).out_dir


def build_staging_assets() -> tuple[Path, AssetBuildResult]:
    """Build assets into a private staging directory for wheel packaging.

    Reuses freshly published checkout assets when their fingerprint matches;
    otherwise performs a full build without touching the checkout's published
    directory. The caller owns the returned staging directory.
    """
    web_dir = find_web_source_dir()
    if web_dir is None:
        published = published_assets_dir()
        if published is None:
            raise AssetBuildError(
                "cannot package the wheel without UI assets and without the web "
                "sources; build this wheel from a full source checkout"
            )
        stage = Path(tempfile.mkdtemp(prefix=_BUILD_STAGE_PREFIX))
        shutil.copytree(published, stage, dirs_exist_ok=True)
        return stage, AssetBuildResult(
            out_dir=stage, rebuilt=False, npm_install=False, fingerprint="copied"
        )
    cached = read_cached_fingerprint(_package_dir() / ASSETS_PACKAGE_SUBDIR)
    if cached is not None:
        try:
            if cached.get("fingerprint") == compute_source_fingerprint(web_dir):
                stage = Path(tempfile.mkdtemp(prefix=_BUILD_STAGE_PREFIX))
                shutil.copytree(
                    _package_dir() / ASSETS_PACKAGE_SUBDIR, stage, dirs_exist_ok=True
                )
                return stage, AssetBuildResult(
                    out_dir=stage,
                    rebuilt=False,
                    npm_install=False,
                    fingerprint=str(cached.get("fingerprint")),
                )
        except AssetBuildError:
            pass
    stage = Path(tempfile.mkdtemp(prefix=_BUILD_STAGE_PREFIX))
    result = build_assets_into(web_dir, stage)
    return stage, result
