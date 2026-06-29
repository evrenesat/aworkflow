# Library API

You can use `aflow` as a Python library instead of invoking the CLI. The public API is available under `aflow.api` and re-exported from the top-level `aflow` package for stable imports.

## Startup and Execution

Startup preparation returns either a `PreparedRun` ready to execute or a `StartupQuestion` that needs user input.

```python
from pathlib import Path
from aflow import (
    StartupRequest,
    StartupQuestion,
    prepare_startup,
    prepare_startup_with_answer,
    execute_workflow,
)

request = StartupRequest(
    repo_root=Path("."),
    plan_path=Path("plans/my-plan.md"),
    workflow_name="ralph",
    start_step=None,
)

result = prepare_startup(request)

if isinstance(result, StartupQuestion):
    answer = input(f"{result.message}: ")
    result = prepare_startup_with_answer(request, answer)

run_result = execute_workflow(result)
print(f"Run completed: {run_result.end_reason}")
```

When startup preparation returns a `StartupQuestion`, the caller decides how to present it. The CLI renders questions as TTY prompts; non-CLI callers can present them in any UI or answer programmatically through `prepare_startup_with_answer()`.

## Custom Observers

Use `WorkflowRunner` with a custom observer for more control over execution.

```python
from aflow import WorkflowRunner, RunnerConfig, CallbackObserver, ExecutionEvent

def my_observer(event: ExecutionEvent) -> None:
    print(f"Event: {event.event_type}")

config = RunnerConfig(
    prepared_run=result,
    observer=CallbackObserver(my_observer),
)

runner = WorkflowRunner(config)
run_result = runner.run()
```

## Run Analysis

Run analysis is also available through the public Python API, so callers do not need to shell out to `aflow analyze`.

```python
from pathlib import Path
from aflow.api import AnalyzeRequest, analyze_runs

payload = analyze_runs(AnalyzeRequest(repo_root=Path("."), run_id="20260407-121715"))
```

`AnalyzeRequest` uses the same single-run and corpus behavior as the CLI. Set `all=True` for corpus mode.

## Public Types

The stable public API includes:

- `StartupRequest`
- `StartupQuestion`
- `PreparedRun`
- `ExecutionObserver`
- `CallbackObserver`
- `CollectingObserver`
- `ExecutionEvent`
- `WorkflowRunner`
- `RunnerConfig`
- `prepare_startup`
- `prepare_startup_with_answer`
- `execute_workflow`
- `AnalyzeRequest`
- `analyze_runs`

See [Architecture](../ARCHITECTURE.md) for module-level notes and model details.
