from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def disable_repository_publication_for_synthetic_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep ordinary synthetic workflow tests from inheriting checkout grants."""
    monkeypatch.setattr(
        "aflow.workflow.publish_completed_run",
        lambda *args, **kwargs: None,
    )
