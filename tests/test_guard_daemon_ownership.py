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
    assert "- `ui-server`: run owned by `aflow ui`/`aflow ui --daemon` persistent units" in skill
    assert "- `aflowd`: production control-plane run owned by its exact systemd unit." in skill
    assert "Never add a tmux or CLI controller to either server-owned mode." in skill
    assert "Use the bundled snapshot only for\n   `legacy` runs." in skill
    assert "Use one authenticated `get_run` through the advertised `/mcp` or `/mcp/`" in skill
    assert "Never use browser cookies or create a disposable transport" in skill
    assert "Use one authenticated `get_run` through the advertised MCP endpoint." in skill
    assert "`aflow-run-<run-id>.service` unit" in skill
    assert "One-shot neutral startup exception" in skill
    assert "normal selected launch path" in skill
    assert "Never add a CLI or tmux controller to a server-owned run." in skill
    assert "Deployment is the sole post-launch mutation and is allowed only when the user" in skill
    assert "explicitly authorized it for this run and the terminal audit passed." in skill
    assert "scripts/aflow_guard_report_input.py" in skill
    assert "unavailable evidence stays unknown" in skill
    assert "guard never\nauthorizes a relaunch or other recovery" in skill
