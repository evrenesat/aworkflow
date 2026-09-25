"""Shared reference, fixture, and browser-capture helpers for UI fidelity tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aflow.control_plane import LaunchManifest, create_launch_manifest, write_launch_phase
from aflow.control_plane.persistence import append_run_event
from playwright.sync_api import Page


REFERENCE_DIR = Path(__file__).resolve().parents[4] / "docs" / "ui-reference" / "calm-workspace"
DEMO_PATH = REFERENCE_DIR / "demo.html"
MANIFEST_PATH = REFERENCE_DIR / "manifest.json"
MEASUREMENTS_PATH = REFERENCE_DIR / "measurements.json"
FIXTURES_PATH = REFERENCE_DIR / "fixtures.json"
EXPECTED_DEMO_SHA256 = "2465c0ac2bfef90af2b98a537ad5ac3e931b927c23a78379a8c532ce4dcbadf4"
CAPTURE_VIEWPORTS = ((1280, 720), (1440, 900), (390, 844))
CAPTURE_THEMES = ("light", "dark")
REFERENCE_ANCHORS = {
    "title": "#af-run-name",
    "current_work": ".af-now",
    "latest_result": ".af-latest",
    "recent_activity": "#af-events",
    "first_disclosure": "details[data-explore]",
}
PRODUCTION_ANCHORS = {
    "title": ".run-detail h3",
    "current_work": "[data-ui-fidelity-anchor='current-work']",
    "latest_result": "[data-ui-fidelity-anchor='latest-result']",
    "recent_activity": "[data-ui-fidelity-anchor='recent-activity']",
    "mobile_recent_activity": "[data-ui-fidelity-anchor='mobile-recent-activity']",
    "first_disclosure": "details[data-ui-fidelity-anchor='first-disclosure']",
}
PRODUCTION_PROGRESS_SURFACE = ".run-progress-evidence"


def load_reference_manifest() -> dict[str, Any]:
    """Load and validate the versioned reference manifest before a capture."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    observed = hashlib.sha256(DEMO_PATH.read_bytes()).hexdigest()
    expected = manifest.get("demo_sha256")
    if expected != EXPECTED_DEMO_SHA256 or observed != EXPECTED_DEMO_SHA256:
        raise AssertionError(
            "Frozen calm-workspace reference checksum changed: "
            f"expected {EXPECTED_DEMO_SHA256}, observed {observed}"
        )
    if manifest.get("anchors") != REFERENCE_ANCHORS:
        raise AssertionError("Reference manifest anchors do not match the harness contract")
    expected_matrix = [
        {"width": width, "height": height, "theme": theme}
        for width, height in CAPTURE_VIEWPORTS
        for theme in CAPTURE_THEMES
    ]
    if manifest.get("capture_matrix") != expected_matrix:
        raise AssertionError("Reference capture matrix does not match the harness contract")
    measurements = json.loads(MEASUREMENTS_PATH.read_text(encoding="utf-8"))
    if measurements.get("recorded_reference_sha256") != EXPECTED_DEMO_SHA256:
        raise AssertionError("Versioned reference measurements use a different demo checksum")
    if tuple(measurements.get("anchor_order", ())) != tuple(REFERENCE_ANCHORS):
        raise AssertionError("Versioned reference measurements use a different anchor order")
    return manifest


def load_fixture_spec() -> dict[str, Any]:
    """Return deterministic demo-shaped data used only by disposable tests."""
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _plan_body(variant: dict[str, Any]) -> str:
    approved = set(variant["approved_checkpoints"])
    lines = [f"# {variant['title']}", ""]
    for index, name in enumerate(variant["checkpoint_names"], start=1):
        mark = "x" if index in approved else " "
        lines.extend(
            (
                f"### [{mark}] Checkpoint {index}: {name}",
                f"- [{mark}] Record evidence for {name}",
                "",
            )
        )
    return "\n".join(lines)


def _turn_record(
    *,
    run_dir: Path,
    plan_path: Path,
    turn: int,
    checkpoint: int,
    role: str,
    selector: str,
    status: str,
    outcome: str,
) -> dict[str, Any]:
    step_name = "implement" if role == "worker" else "review"
    record: dict[str, Any] = {
        "turn_number": turn,
        "step_name": step_name,
        "step_role": role,
        "role": role,
        "selector": selector,
        "status": status,
        "outcome": outcome,
        "started_at": f"2026-09-22T10:{turn:02d}:00Z",
        "original_plan_path": str(plan_path),
        "checkpoint_index": checkpoint,
    }
    if status not in {"running", "starting"}:
        record["finished_at"] = f"2026-09-22T10:{turn:02d}:05Z"
    _write_json(run_dir / "turns" / f"turn-{turn:03d}" / "result.json", record)
    return record


def seed_demo_fidelity_fixture(root: Path) -> dict[str, Any]:
    """Materialize running, paused, and completed evidence-backed runs.

    The data is written below the disposable ``control_client`` project root.
    It follows the same on-disk run/plan records consumed by the real server;
    no UI mock or production API shortcut is involved.
    """
    spec = load_fixture_spec()
    fixture_ids: dict[str, Any] = {}
    for variant_name, variant in spec["variants"].items():
        plan = root / "plans" / variant["plan_status"] / f"{variant['plan_slug']}.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(_plan_body(variant), encoding="utf-8")

        run_id = variant["run_id"]
        create_launch_manifest(
            root,
            LaunchManifest(
                run_id=run_id,
                project_root=str(root.resolve()),
                plan_path=str(plan.resolve()),
                workflow_name="managed",
                max_turns=variant["max_turns"],
                team=variant["team"],
                start_step="implement",
                idempotency_key=f"ui-demo-fidelity-{variant_name}",
                caller_scope=f"bearer:{spec['project_id']}",
                created_at=variant["run_started_at"],
            ),
        )
        launch_phase = "completed" if variant["status"] == "completed" else "unit_started"
        write_launch_phase(root, run_id, launch_phase)
        run_dir = root / ".aflow" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        current_checkpoint = variant.get("current_checkpoint")
        metadata: dict[str, Any] = {
            "run_id": run_id,
            "status": variant["status"],
            "repo_root": str(root),
            "original_plan_path": str(plan),
            "active_plan_path": str(plan),
            "plan_path": str(plan),
            "workflow_name": "managed",
            "team": variant["team"],
            "current_step_name": variant["current_step_name"],
            "turns_completed": variant["turns_completed"],
            "max_turns": variant["max_turns"],
            "run_started_at": variant["run_started_at"],
            "activity": variant["activity"],
            "phase": variant["phase"],
            "progress_history_complete": True,
            "approved_checkpoints": [
                {
                    "checkpoint_index": index,
                    "status": "approved",
                    "decision_number": index,
                    "source_run_id": run_id,
                    "role": "reviewer",
                    "selector": "codex.astra-medium",
                    "turn_number": index * 2,
                    "recorded_at": f"2026-09-22T10:{index * 2:02d}:05Z",
                    "reason": "The recorded review approved the checkpoint evidence.",
                }
                for index in variant["approved_checkpoints"]
            ],
        }
        if variant.get("status_reason_code"):
            metadata["status_reason_code"] = variant["status_reason_code"]
        if variant.get("reason"):
            metadata["reason"] = variant["reason"]
        if current_checkpoint is not None:
            checkpoint_name = variant["checkpoint_names"][current_checkpoint - 1]
            metadata["active_implementation_scope"] = {
                "scope_id": f"original::checkpoint-{current_checkpoint}",
                "original_plan_path": str(plan),
                "checkpoint_index": current_checkpoint,
                "checkpoint_name": f"Checkpoint {current_checkpoint}: {checkpoint_name}",
                "opened_turn_number": variant["turns_completed"],
                "awaiting_review": variant["active_role"] == "reviewer",
            }

        attempts: dict[str, list[dict[str, Any]]] = {}
        if variant_name == "running":
            turn_specs = (
                (1, 1, "worker", "codex.worker", "completed", "completed"),
                (2, 1, "reviewer", "codex.astra-medium", "completed", "accepted"),
                (3, 2, "worker", "codex.worker", "completed", "completed"),
                (4, 2, "reviewer", "codex.astra-medium", "running", "reviewing"),
            )
        elif variant_name == "paused":
            turn_specs = (
                (1, 1, "worker", "codex.worker", "completed", "completed"),
                (2, 1, "worker", "codex.worker", "failed", "provider_rate_limit"),
            )
        else:
            turn_specs = tuple(
                item
                for index in range(1, len(variant["checkpoint_names"]) + 1)
                for item in (
                    (index * 2 - 1, index, "worker", "codex.worker", "completed", "completed"),
                    (index * 2, index, "reviewer", "codex.astra-medium", "completed", "accepted"),
                )
            )
        for turn, checkpoint, role, selector, status, outcome in turn_specs:
            record = _turn_record(
                run_dir=run_dir,
                plan_path=plan,
                turn=turn,
                checkpoint=checkpoint,
                role=role,
                selector=selector,
                status=status,
                outcome=outcome,
            )
            append_run_event(
                run_dir,
                "turn_started",
                {
                    "turn_number": turn,
                    "step_name": record["step_name"],
                    "step_role": role,
                    "role": role,
                    "selector": selector,
                },
            )
            if status not in {"running", "starting"}:
                append_run_event(
                    run_dir,
                    "turn_finished",
                    {
                        "turn_number": turn,
                        "step_name": record["step_name"],
                        "step_role": role,
                        "role": role,
                        "selector": selector,
                        "status": status,
                        "outcome": outcome,
                        "returncode": 0 if status == "completed" else 1,
                    },
                )
            attempts.setdefault(f"original::checkpoint-{checkpoint}", []).append(record)
        metadata["implementation_attempts"] = attempts
        _write_json(run_dir / "run.json", metadata)
        _write_json(run_dir / "publication.json", {"stages": variant["publication"]})
        fixture_ids[variant_name] = {
            "run_id": run_id,
            "title": variant["title"],
            "plan": str(plan),
            "status": variant["status"],
            "launch_manifest": str(root / ".aflow" / "launches" / f"{run_id}.json"),
            "launch_phase": launch_phase,
            "launch_state": str(root / ".aflow" / "launches" / f"{run_id}.state.json"),
        }
    return fixture_ids


def measure_named_anchors(page: Page, anchors: dict[str, str] | None = None) -> dict[str, Any]:
    """Measure named anchors, including CSS visibility and rendered geometry."""
    measured: dict[str, Any] = {}
    for name, selector in (anchors or REFERENCE_ANCHORS).items():
        matches = page.locator(selector)
        match_count = matches.count()
        if match_count == 0:
            measured[name] = {
                "selector": selector,
                "match_count": 0,
                "present": False,
                "visible": False,
                "box": None,
            }
            continue
        locator = matches.first
        visible = locator.is_visible()
        box = locator.bounding_box()
        measured[name] = {
            "selector": selector,
            "match_count": match_count,
            "present": True,
            "visible": visible,
            "box": box,
        }
    return measured


def capture_reference_surface(
    page: Page,
    *,
    width: int,
    height: int,
    theme: str,
    screenshot_path: Path,
) -> dict[str, Any]:
    """Render the literal fragment and capture only its product surface."""
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(color_scheme=theme)  # type: ignore[arg-type]
    page_errors: list[str] = []

    def record_page_error(error: Exception) -> None:
        page_errors.append(str(error))

    page.on("pageerror", record_page_error)
    try:
        page.goto(DEMO_PATH.as_uri(), wait_until="load")
        frame = page.locator("#af-frame")
        frame.wait_for()
        footer = frame.locator(".af-footer")
        if footer.count():
            footer.evaluate("element => { element.style.display = 'none' }")
        try:
            frame.screenshot(path=str(screenshot_path))
        finally:
            if footer.count():
                footer.evaluate("element => { element.style.display = '' }")
        return {
            "source": DEMO_PATH.name,
            "surface_selector": "#af-frame",
            "width": width,
            "height": height,
            "theme": theme,
            "page_errors": page_errors,
            "anchors": measure_named_anchors(page),
        }
    finally:
        page.remove_listener("pageerror", record_page_error)


def install_mutation_observer(page: Page, root_selector: str = "body") -> None:
    """Install a bounded observer for later unchanged-refresh assertions."""
    page.evaluate(
        """selector => {
          const root = document.querySelector(selector);
          if (!root) throw new Error(`Required mutation root missing: ${selector}`);
          window.__aflowFidelityMutations = [];
          window.__aflowFidelityMutationObserver?.disconnect();
          const observer = new MutationObserver(records => {
            window.__aflowFidelityMutations.push(...records.slice(0, 100).map(record => ({
              type: record.type,
              target: record.target.nodeName,
              added: record.addedNodes.length,
              removed: record.removedNodes.length,
            })));
            window.__aflowFidelityMutations = window.__aflowFidelityMutations.slice(-100);
          });
          observer.observe(root, {subtree: true, childList: true, attributes: true, characterData: true});
          window.__aflowFidelityMutationObserver = observer;
        }""",
        root_selector,
    )


def read_mutation_records(page: Page) -> list[dict[str, Any]]:
    """Return and clear observer records so each refresh has a bounded sample."""
    return page.evaluate(
        """() => {
          const records = window.__aflowFidelityMutations || [];
          window.__aflowFidelityMutations = [];
          return records;
        }"""
    )


def capture_dom_identity(page: Page, selector: str) -> dict[str, Any]:
    """Capture node identity, text, geometry, focus, and document scroll."""
    return page.evaluate(
        """selector => {
          const element = document.querySelector(selector);
          if (!element) return {present: false};
          window.__aflowFidelityNodeTokens ||= new WeakMap();
          window.__aflowFidelityNextNodeToken ||= 0;
          let nodeToken = window.__aflowFidelityNodeTokens.get(element);
          if (!nodeToken) {
            nodeToken = `node-${++window.__aflowFidelityNextNodeToken}`;
            window.__aflowFidelityNodeTokens.set(element, nodeToken);
          }
          const rect = element.getBoundingClientRect();
          return {
            present: true,
            nodeName: element.nodeName,
            identity: nodeToken,
            text: element.textContent || '',
            x: rect.x,
            y: rect.y,
            width: rect.width,
            height: rect.height,
            focused: element === document.activeElement,
            scrollY: window.scrollY,
          };
        }""",
        selector,
    )


def install_refresh_probe(page: Page, roots: dict[str, str]) -> None:
    """Observe exact visible roots without changing DOM, styles, or layout.

    The WeakMap token is assigned to an actual Element, never an HTML id.
    MutationObserver callbacks only append bounded records in JS memory. A
    parent observer detects root replacement, which a subtree observer alone
    cannot see. Snapshot reads only geometry and state, so instrumentation
    itself cannot cause a repaint.
    """
    page.evaluate(
        """selectors => {
          window.__aflowRefreshProbe?.observers.forEach(observer => observer.disconnect());
          const tokens = new WeakMap();
          let nextToken = 0;
          const token = node => {
            if (!node) return null;
            if (!tokens.has(node)) tokens.set(node, ++nextToken);
            return tokens.get(node);
          };
          const locate = (name, selector) => {
            const matches = [...document.querySelectorAll(selector)].filter(node => {
              const box = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              return node.isConnected && box.width > 0 && box.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden';
            });
            if (matches.length !== 1) throw new Error(`Expected one visible ${name} root (${selector}), got ${matches.length}`);
            return matches[0];
          };
          const records = [];
          const observers = [];
          const nodes = Object.fromEntries(Object.entries(selectors).map(([name, selector]) =>
            [name, locate(name, selector)]));
          for (const [name, root] of Object.entries(nodes)) {
            const observer = new MutationObserver(changes => {
              for (const change of changes) {
                const target = change.target.nodeType === Node.ELEMENT_NODE ? change.target : change.target.parentElement;
                const addedElements = [...change.addedNodes].filter(node => node.nodeType === Node.ELEMENT_NODE);
                const removedElements = [...change.removedNodes].filter(node => node.nodeType === Node.ELEMENT_NODE);
                const clockLabel = target?.closest?.('dd')?.previousElementSibling?.textContent ?? '';
                const classification = addedElements.length || removedElements.length ? 'subtree'
                  : change.type === 'characterData' && /elapsed|duration|running for/i.test(clockLabel + ' ' + (target?.textContent ?? '')) ? 'clock'
                  : change.type === 'characterData' ? 'value'
                  : change.attributeName === 'hidden' || change.attributeName === 'aria-hidden' ? 'visibility'
                  : 'attribute';
                records.push({root: name, type: change.type, target: target?.tagName ?? '#text',
                  targetToken: token(target), attribute: change.attributeName,
                  classification, added: addedElements.length, removed: removedElements.length,
                  addedElements: addedElements.map(node => ({tag: node.tagName,
                    className: String(node.className).slice(0, 100),
                    text: (node.textContent ?? '').slice(0, 120)})),
                  removedElements: removedElements
                    .map(node => ({tag: node.tagName, className: String(node.className).slice(0, 100),
                      text: (node.textContent ?? '').slice(0, 120)}))});
              }
              if (records.length > 500) {
                window.__aflowRefreshProbe.overflow = true;
                records.splice(0, records.length - 500);
              }
            });
            observer.observe(root, {subtree: true, childList: true, attributes: true, characterData: true});
            observers.push(observer);
            if (root.parentElement) {
              const parent = new MutationObserver(changes => {
                if (changes.some(change => [...change.removedNodes].some(node => node === root || node.contains?.(root)))) {
                  records.push({root: name, type: 'root-removed', classification: 'root-replacement'});
                }
              });
              parent.observe(root.parentElement, {childList: true});
              observers.push(parent);
            }
          }
          window.__aflowRefreshProbe = {selectors, nodes, token, locate, records, observers, overflow: false};
        }""",
        roots,
    )


def capture_refresh_probe(page: Page) -> dict[str, Any]:
    """Read mounted-node, geometry, scroll, focus and disclosure continuity."""
    return page.evaluate(
        """() => {
          const probe = window.__aflowRefreshProbe;
          if (!probe) throw new Error('Refresh probe was not installed');
          const active = document.activeElement;
          const focused = active instanceof Element ? probe.token(active) : null;
          const roots = Object.fromEntries(Object.entries(probe.selectors).map(([name, selector]) => {
            const node = probe.locate(name, selector);
            const box = node.getBoundingClientRect();
            return [name, {token: probe.token(node), sameNode: node === probe.nodes[name],
              connected: node.isConnected, text: node.textContent ?? '',
              open: node instanceof HTMLDetailsElement ? node.open : null,
              box: {x: box.x, y: box.y, width: box.width, height: box.height},
              scrollTop: node.scrollTop,
              disclosures: [...node.querySelectorAll('details')].map(detail =>
                ({token: probe.token(detail), open: detail.open})),
            }];
          }));
          return {roots, focused, scrollX: window.scrollX, scrollY: window.scrollY,
            documentScrollTop: document.scrollingElement?.scrollTop ?? 0};
        }"""
    )


def read_refresh_probe_mutations(page: Page) -> list[dict[str, Any]]:
    """Drain mutations after queued observer callbacks have run."""
    return page.evaluate(
        """async () => {
          await Promise.resolve();
          const probe = window.__aflowRefreshProbe;
          if (!probe) throw new Error('Refresh probe was not installed');
          if (probe.overflow) throw new Error('Refresh probe overflowed its mutation log');
          const records = [...probe.records];
          probe.records.length = 0;
          return records;
        }"""
    )
