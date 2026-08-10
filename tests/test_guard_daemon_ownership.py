from pathlib import Path


def test_guard_guidance_excludes_daemon_owned_runs_but_retains_legacy_and_sre_paths() -> None:
    skill = (
        Path(__file__).resolve().parents[1]
        / "aflow"
        / "bundled_skills"
        / "aflow-guard-development-run"
        / "SKILL.md"
    ).read_text()

    assert "Never attach a ChatGPT\n  heartbeat guard" in skill
    assert "authenticated control plane" in skill
    assert "Legacy/manual runs" in skill
    assert "explicitly requested SRE stabilization" in skill
