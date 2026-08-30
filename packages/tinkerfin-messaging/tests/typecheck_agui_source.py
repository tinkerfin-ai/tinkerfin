"""Static contracts for the task-level AG-UI source helper."""

from ag_ui.core import BaseEvent

from tinkerfin_contracts import RunIdentity
from tinkerfin_messaging import MessageSource, create_agui_run_source

identity = RunIdentity(threadId="type-thread", runId="type-run")


async def open_events(_identity: RunIdentity) -> MessageSource[BaseEvent]:
    """Represent one correctly typed lazy managed event source."""

    raise RuntimeError("static fixture")


async def open_object(_identity: RunIdentity) -> object:
    """Represent an invalid opener that loses the source contract."""

    return object()


source = create_agui_run_source(identity, open_events=open_events)
invalid_source = create_agui_run_source(
    identity,
    open_events=open_object,  # pyright: ignore[reportArgumentType]
)

assert isinstance(source, MessageSource)
assert isinstance(invalid_source, MessageSource)
