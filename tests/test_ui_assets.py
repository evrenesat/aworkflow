from __future__ import annotations

from pathlib import Path

from aflow import ui_assets


def _web_checkout(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    web = repository / "apps" / "aflow_app" / "web"
    (web / "src").mkdir(parents=True)
    (web / "scripts").mkdir()
    (web / "package.json").write_text('{"scripts":{"build":"vite"}}\n', encoding="utf-8")
    (web / "package-lock.json").write_text(
        '{"name":"fixture","lockfileVersion":3}\n', encoding="utf-8"
    )
    (web / "src" / "App.tsx").write_text("export {};\n", encoding="utf-8")
    (web / "scripts" / "generate-changelog.mjs").write_text(
        "export {};\n", encoding="utf-8"
    )
    (repository / "DEVLOG.md").write_text(
        "## 2026-09-10 — Initial release\n", encoding="utf-8"
    )
    return repository, web


def test_devlog_only_changes_invalidate_the_source_fingerprint(tmp_path: Path) -> None:
    repository, web = _web_checkout(tmp_path)

    original = ui_assets.compute_source_fingerprint(web)
    (repository / "DEVLOG.md").write_text(
        "## 2026-09-10 — Initial release\n\n## 2026-09-11: New release\n",
        encoding="utf-8",
    )

    assert ui_assets.compute_source_fingerprint(web) != original


def test_generated_changelog_does_not_invalidate_the_fingerprint(tmp_path: Path) -> None:
    _, web = _web_checkout(tmp_path)

    original = ui_assets.compute_source_fingerprint(web)
    generated = web / "src" / "generated" / "changelog.json"
    generated.parent.mkdir()
    generated.write_text('{"schema_version":1}\n', encoding="utf-8")

    assert ui_assets.compute_source_fingerprint(web) == original


def test_unchanged_assets_are_cached_and_devlog_changes_rebuild(
    tmp_path: Path, monkeypatch
) -> None:
    repository, web = _web_checkout(tmp_path)
    package_dir = tmp_path / "package" / "aflow"
    package_dir.mkdir(parents=True)
    build_calls: list[str] = []

    monkeypatch.setattr(ui_assets, "_package_dir", lambda: package_dir)
    monkeypatch.setattr(ui_assets, "find_web_source_dir", lambda: web)

    def fake_build_assets_into(web_dir: Path, out_dir: Path, *, progress=None):
        fingerprint = ui_assets.compute_source_fingerprint(web_dir)
        build_calls.append(fingerprint)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        ui_assets._write_fingerprint(out_dir, fingerprint, npm_install=False)
        return ui_assets.AssetBuildResult(
            out_dir=out_dir,
            rebuilt=True,
            npm_install=False,
            fingerprint=fingerprint,
        )

    monkeypatch.setattr(ui_assets, "build_assets_into", fake_build_assets_into)

    first = ui_assets.ensure_checkout_assets()
    second = ui_assets.ensure_checkout_assets()
    assert first.rebuilt is True
    assert second.rebuilt is False
    assert len(build_calls) == 1

    (repository / "DEVLOG.md").write_text(
        "## 2026-09-10 — Initial release\n\n## 2026-09-11: New release\n",
        encoding="utf-8",
    )
    third = ui_assets.ensure_checkout_assets()
    assert third.rebuilt is True
    assert len(build_calls) == 2
    assert build_calls[0] != build_calls[1]
