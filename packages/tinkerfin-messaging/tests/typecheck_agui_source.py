"""Static contracts for a directly supplied AG-UI event source."""

from ag_ui.core import BaseEvent

from tinkerfin_messaging import (
    MessageSource,
    ProfiledMessageSource,
    create_agui_run_source,
)


def check_source_types(
    events: ProfiledMessageSource[BaseEvent, BaseEvent],
    unprofiled: MessageSource[BaseEvent],
    wrong_replay: ProfiledMessageSource[BaseEvent, str],
) -> ProfiledMessageSource[BaseEvent, BaseEvent]:
    create_agui_run_source(unprofiled)  # pyright: ignore[reportArgumentType]
    create_agui_run_source(wrong_replay)  # pyright: ignore[reportArgumentType]
    return create_agui_run_source(events)
