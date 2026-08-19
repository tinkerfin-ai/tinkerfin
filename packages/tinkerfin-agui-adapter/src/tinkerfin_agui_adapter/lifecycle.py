"""AG-UI main-run lifecycle event construction."""

from __future__ import annotations

from ag_ui.core import (
    BaseEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
)

from .contracts import AgentRunOutcome


class AgUiLifecycleEventFactory:
    """Build exactly-one-owner main lifecycle events from complete caller input.

    The factory is stateless: it does not decide terminal ownership, access a
    graph or checkpointer, or send events over a transport. Callers must emit a
    start before converted child events, then one finished or failed terminal
    after all open child lifecycles have been closed.
    """

    def started(
        self,
        *,
        run_input: RunAgentInput,
    ) -> BaseEvent:
        """Build the main start event from the caller's complete AG-UI input."""

        self.validate_run_input(run_input)
        return RunStartedEvent(
            thread_id=run_input.thread_id,
            run_id=run_input.run_id,
            parent_run_id=run_input.parent_run_id,
            input=run_input.model_copy(deep=True),
        )

    @staticmethod
    def validate_run_input(run_input: RunAgentInput) -> None:
        """Reject invalid AG-UI identity before a runtime performs side effects."""

        if not isinstance(run_input, RunAgentInput):
            raise TypeError("run_input must be a RunAgentInput")
        for name, value in (
            ("thread_id", run_input.thread_id),
            ("run_id", run_input.run_id),
        ):
            if not value or value != value.strip():
                raise ValueError(
                    f"{name} must be non-blank without surrounding whitespace"
                )
        parent_run_id = run_input.parent_run_id
        if parent_run_id is not None and (
            not parent_run_id or parent_run_id != parent_run_id.strip()
        ):
            raise ValueError(
                "parent_run_id must be non-blank without surrounding whitespace"
            )

    def finished(
        self,
        *,
        thread_id: str,
        run_id: str,
        outcome: AgentRunOutcome,
    ) -> BaseEvent:
        """Build a success or interrupt terminal from an adapter outcome."""

        if outcome.type == "interrupt":
            run_outcome = RunFinishedInterruptOutcome(
                interrupts=list(outcome.interrupts)
            )
        else:
            run_outcome = RunFinishedSuccessOutcome()
        return RunFinishedEvent(
            thread_id=thread_id,
            run_id=run_id,
            outcome=run_outcome,
        )

    def failed(
        self,
        *,
        run_id: str,
        message: str,
        code: str,
    ) -> BaseEvent:
        """Build a main error terminal identified by the explicit run ID."""

        return RunErrorEvent(
            message=message,
            code=code,
            raw_event={"runId": run_id},
        )

    def is_main_lifecycle(self, event: BaseEvent, *, run_id: str) -> bool:
        """Return whether an event competes for the named main lifecycle."""

        return (
            isinstance(
                event,
                RunStartedEvent | RunFinishedEvent | RunErrorEvent,
            )
            and self.event_run_id(event) == run_id
        )

    @staticmethod
    def event_run_id(event: BaseEvent) -> str | None:
        """Read a run ID from the standard field or validated `rawEvent`."""

        run_id = getattr(event, "run_id", None)
        if isinstance(run_id, str) and run_id:
            return run_id
        raw_event = event.raw_event
        if not isinstance(raw_event, dict):
            return None
        raw_run_id = raw_event.get("runId")
        return raw_run_id if isinstance(raw_run_id, str) else None
