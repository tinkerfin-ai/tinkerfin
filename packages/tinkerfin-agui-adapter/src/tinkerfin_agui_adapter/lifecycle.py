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

from .contracts import AgentRunOutcome, Identity


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
        identity: Identity,
    ) -> BaseEvent:
        """Build the main start event from the canonical runtime identity."""

        self.validate_identity(identity)
        return RunStartedEvent(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            input=None,
        )

    @staticmethod
    def validate_identity(identity: Identity) -> None:
        """Reject values outside the shared Identity contract."""

        if not isinstance(identity, Identity):
            raise TypeError("identity must be an Identity")

    def finished(
        self,
        *,
        identity: Identity,
        outcome: AgentRunOutcome,
    ) -> BaseEvent:
        """Build a success or interrupt terminal from an adapter outcome."""

        self.validate_identity(identity)
        if outcome.type == "interrupt":
            run_outcome = RunFinishedInterruptOutcome(
                interrupts=list(outcome.interrupts)
            )
        else:
            run_outcome = RunFinishedSuccessOutcome()
        return RunFinishedEvent(
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            outcome=run_outcome,
        )

    def failed(
        self,
        *,
        identity: Identity,
        message: str,
        code: str,
    ) -> BaseEvent:
        """Build a main error terminal from the canonical runtime identity."""

        self.validate_identity(identity)
        return RunErrorEvent(
            message=message,
            code=code,
            raw_event={
                "threadId": identity.thread_id,
                "runId": identity.run_id,
            },
        )

    def is_main_lifecycle(self, event: BaseEvent, *, identity: Identity) -> bool:
        """Return whether an event competes for the identified main lifecycle."""

        self.validate_identity(identity)
        return (
            isinstance(
                event,
                RunStartedEvent | RunFinishedEvent | RunErrorEvent,
            )
            and self.event_run_id(event) == identity.run_id
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
