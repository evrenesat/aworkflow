from __future__ import annotations

import json
from pathlib import Path

from .base import HarnessInvocation
from .preflight import (
    HarnessEnvironmentBlocker,
    HarnessPreflightContext,
    HarnessPreflightProbe,
    REASONIX_BWRAP_REMEDIATION,
    diagnostic_fields,
)


class ReasonixAdapter:
    """Harness adapter for the Reasonix AI coding agent.

    Reasonix does not expose an ``--effort`` flag; reasoning effort is baked
    into each model variant (e.g. ``deepseek-flash`` vs ``deepseek-pro-max``),
    so ``supports_effort`` is ``False``.

    Permission bypass is handled through the Reasonix config file (``reasonix.toml``)
    rather than CLI flags — the user should set ``[permissions] mode = "allow"``
    or appropriate allow rules for non-interactive use.
    """

    name = "reasonix"
    supports_effort = False

    def build_invocation(
        self,
        *,
        repo_root: Path,
        model: str | None,
        system_prompt: str,
        user_prompt: str,
        effort: str | None = None,
    ) -> HarnessInvocation:
        effective_prompt = "\n\n".join((system_prompt, user_prompt))
        # Current Reasonix releases expose the directory option only as
        # ``--dir``.  The old single-dash spelling exits during argument
        # parsing before the agent can produce diagnostics.
        argv: list[str] = ["reasonix", "run", "--dir", str(repo_root)]
        if model is not None:
            argv.extend(["--model", model])
        argv.append(effective_prompt)
        final_output_argv = [*argv[:-1], "--print", effective_prompt]
        return HarnessInvocation(
            label=self.name,
            argv=tuple(argv),
            env={},
            prompt_mode="prefix-system-into-user-prompt",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            effective_prompt=effective_prompt,
            final_output_argv=tuple(final_output_argv),
        )

    def preflight_environment(
        self,
        context: HarnessPreflightContext,
        probe: HarnessPreflightProbe,
    ) -> HarnessEnvironmentBlocker | None:
        resolved = probe.resolve_executable(context.invocation.argv[0], env=context.env)
        if resolved is None:
            return None
        try:
            result = probe.run_diagnostic(
                (resolved, "doctor", "--json"),
                cwd=context.cwd,
                env=context.env,
                timeout_seconds=5.0,
            )
        except (OSError, NotImplementedError, TimeoutError, TypeError, ValueError):
            return None
        returncode, stdout, timed_out = diagnostic_fields(result)
        if timed_out or returncode != 0 or not stdout:
            return None
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError):
            return None
        sandbox = payload.get("sandbox") if isinstance(payload, dict) else None
        if not isinstance(sandbox, dict) or sandbox.get("bash") != "enforce":
            return None
        if probe.resolve_executable("bwrap", env=context.env) is not None:
            return None
        return HarnessEnvironmentBlocker(
            "harness_environment_preflight",
            "reasonix_sandbox_bwrap_missing",
            self.name,
            "bwrap",
            ("reasonix", "doctor", "--json"),
            REASONIX_BWRAP_REMEDIATION,
            {"sandbox_bash": "enforce"},
        )
