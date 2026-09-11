"""aflow workflow runner.

Public library API for workflow startup and execution:

    from aflow import StartupRequest, prepare_startup, execute_workflow

    request = StartupRequest(...)
    result = prepare_startup(request)
    # result is either a PreparedRun (ready to execute) or a StartupQuestion (needs user input)

    if isinstance(result, PreparedRun):
        run_result = execute_workflow(result)
"""

from .api import (
    CallbackObserver,
    CollectingObserver,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionObserver,
    PreparedRun,
    RecoveryRequest,
    PLAN_ADMISSION_ERROR_CODE,
    PLAN_ADMISSION_SAFE_MESSAGE,
    PlanAdmissionError,
    QuestionRequiredEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunStartedEvent,
    RunnerConfig,
    StartupError,
    StartupQuestion,
    StartupQuestionKind,
    StartupRequest,
    StatusChangedEvent,
    TurnFinishedEvent,
    TurnStartedEvent,
    WorkflowRunner,
    execute_workflow,
    prepare_startup,
    prepare_startup_with_answer,
)

__all__ = [
    "PreparedRun",
    "RecoveryRequest",
    "PlanAdmissionError",
    "PLAN_ADMISSION_ERROR_CODE",
    "PLAN_ADMISSION_SAFE_MESSAGE",
    "StartupError",
    "StartupQuestion",
    "StartupQuestionKind",
    "StartupRequest",
    "prepare_startup",
    "prepare_startup_with_answer",
    "execute_workflow",
    "WorkflowRunner",
    "RunnerConfig",
    "ExecutionEvent",
    "ExecutionEventType",
    "ExecutionObserver",
    "CallbackObserver",
    "CollectingObserver",
    "RunStartedEvent",
    "StatusChangedEvent",
    "TurnStartedEvent",
    "TurnFinishedEvent",
    "QuestionRequiredEvent",
    "RunCompletedEvent",
    "RunFailedEvent",
]
