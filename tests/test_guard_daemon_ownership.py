from pathlib import Path


def test_guard_guidance_uses_ownership_matched_observation_contract() -> None:
    skill = (
        Path(__file__).resolve().parents[1]
        / "aflow"
        / "bundled_skills"
        / "aflow-guard-development-run"
        / "SKILL.md"
    ).read_text()

    assert "- `legacy`: direct `aflow run` controller, optionally attached to tmux;" in skill
    assert "- `local-daemon`: lightweight `aflow daemon` worker owned by that daemon;" in skill
    assert "- `aflowd`: production control-plane run owned by its exact systemd unit." in skill
    assert "Never add a tmux or CLI controller to either daemon-owned mode." in skill
    assert "Use the bundled snapshot only for\n   `legacy` runs." in skill
    assert "Use `aflow daemon status --repo-root <guarded-repo>` to corroborate" in skill
    assert "Use the daemon's already configured MCP transport for one `get_run`." in skill
    assert "Use one authenticated `get_run` through the advertised MCP endpoint." in skill
    assert "`aflow-run-<run-id>.service` unit" in skill
    assert "Deployment is the sole post-launch mutation and is allowed only when the user" in skill
    assert "explicitly authorized it for this run and the terminal audit passed." in skill
