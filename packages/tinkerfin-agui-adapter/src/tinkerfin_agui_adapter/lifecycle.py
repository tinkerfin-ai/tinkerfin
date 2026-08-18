"""AG-UI main-run lifecycle event construction."""

from __future__ import annotations

from ag_ui.core import (
    BaseEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
)

from .contracts import AgentRunOutcome


class AgUiLifecycleEventFactory:
    """Build exactly-one-owner main lifecycle events from explicit identity.

    The factory is stateless: it does not decide terminal ownership, access a
    graph or checkpointer, or send events over a transport. Callers must emit a
    start before converted child events, then one finished or failed terminal
    after all open child lifecycles have been closed.
    """

    def started(
        self,
        *,
        thread_id: str,
        run_id: str,
        parent_run_id: str | None = None,
    ) -> BaseEvent:
        """Build the main start event without fabricating an AG-UI input."""

        return RunStartedEvent(
            thread_id=thread_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
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
