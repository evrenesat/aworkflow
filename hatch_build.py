"""Hatch build hook: bundle the compiled AFlow UI web assets into the wheel.

The wheel target maps the built web assets into the ``aflow`` package at
``aflow/ui_web/`` so normal installations serve the UI without Node/npm. The
sdist target only makes sure the required web/server sources travel with it;
building from an sdist therefore re-runs this hook with Node available.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class UIAssetsBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        repo_root = Path(__file__).resolve().parent
        sys.path.insert(0, str(repo_root))
        try:
            from aflow.ui_assets import build_staging_assets
        finally:
            sys.path.remove(str(repo_root))

        stage, result = build_staging_assets()
        self._stage = stage
        build_data["force_include"][str(stage)] = "aflow/ui_web"
        if result.rebuilt:
            self.app.display_info(f"built UI web assets into wheel staging: {stage}")

    def finalize(self, version, build_data, artifact_path):
        stage = getattr(self, "_stage", None)
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
            self._stage = None
