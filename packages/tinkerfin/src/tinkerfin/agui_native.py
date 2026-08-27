"""Internal LangGraph invocation binding for strict AG-UI runs."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import cast

from langgraph.types import StreamMode

from tinkerfin_agui_adapter import Identity

from .errors import AgUiNativeStreamConfigurationError

_REQUIRED_MODES: tuple[StreamMode, ...] = ("messages", "tasks", "values")
_SUPPORTED_EXTRA_MODES: frozenset[StreamMode] = frozenset(
    {"updates", "checkpoints", "debug", "custom"}
)


def _bind_graph_identity(
    signature: inspect.Signature,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
    *,
    identity: Identity,
    require_v2: bool,
) -> inspect.BoundArguments:
    """Bind one Graph call and inject its canonical runtime identity."""

    if not isinstance(identity, Identity):
        raise TypeError("identity must be an Identity")
    bound = signature.bind(*args, **kwargs)
    parameters = signature.parameters
    variable_keyword = next(
        (
            name
            for name, parameter in parameters.items()
            if parameter.kind is inspect.Parameter.VAR_KEYWORD
        ),
        None,
    )

    def read(name: str) -> object | None:
        parameter = parameters.get(name)
        if (
            parameter is not None
            and parameter.kind is not inspect.Parameter.VAR_KEYWORD
        ):
            return bound.arguments.get(name)
        if variable_keyword is None:
            return None
        options = bound.arguments.get(variable_keyword)
        return (
            cast(Mapping[object, object], options).get(name)
            if isinstance(options, Mapping)
            else None
        )

    def write(name: str, value: object) -> None:
        parameter = parameters.get(name)
        if (
            parameter is not None
            and parameter.kind is not inspect.Parameter.VAR_KEYWORD
        ):
            bound.arguments[name] = value
            return
        if variable_keyword is None:
            raise TypeError(f"Graph astream must accept the {name!r} option")
        raw_options = bound.arguments.get(variable_keyword)
        options: dict[object, object] = (
            dict(cast(Mapping[object, object], raw_options))
            if isinstance(raw_options, Mapping)
            else {}
        )
        options[name] = value
        bound.arguments[variable_keyword] = options

    raw_config = read("config")
    if raw_config is None:
        config: dict[str, object] = {}
    elif isinstance(raw_config, Mapping):
        config = dict(cast(Mapping[str, object], raw_config))
    else:
        raise TypeError("config must be a mapping or None")
    raw_configurable = config.get("configurable")
    if raw_configurable is None:
        configurable: dict[str, object] = {}
    elif isinstance(raw_configurable, Mapping):
        configurable = dict(cast(Mapping[str, object], raw_configurable))
    else:
        raise TypeError("config.configurable must be a mapping")
    configured_thread = configurable.get("thread_id")
    if configured_thread is not None and configured_thread != identity.thread_id:
        raise ValueError("config thread_id must equal identity.thread_id")
    configurable["thread_id"] = identity.thread_id
    config["configurable"] = configurable
    write("config", config)

    if require_v2:
        version = read("version")
        if version is not None and version != "v2":
            raise ValueError("native TinkerFin Runtime requires version='v2'")
        write("version", "v2")
    return bound


def _bind_agui_graph_astream(  # pyright: ignore[reportUnusedFunction]
    astream: Callable[..., AsyncIterator[Mapping[str, object]]],
    /,
    *args: object,
    **options: object,
) -> Callable[[], AsyncIterator[Mapping[str, object]]]:
    """Validate preserved native arguments and build a strict AG-UI invocation."""

    if not callable(astream):
        raise TypeError("astream must be callable")
    forwarded = dict(options)
    if "stream_mode" not in forwarded:
        extra_modes: tuple[StreamMode, ...] = ()
    else:
        raw_modes = forwarded.pop("stream_mode")
        if raw_modes is None:
            raise AgUiNativeStreamConfigurationError(
                "AG-UI stream_mode must include messages, tasks, and values"
            )
        if isinstance(raw_modes, str):
            modes: tuple[object, ...] = (raw_modes,)
        elif isinstance(raw_modes, Sequence):
            modes = tuple(cast(Sequence[object], raw_modes))
        else:
            raise AgUiNativeStreamConfigurationError(
                "AG-UI stream_mode must be a stream mode or sequence of modes"
            )
        duplicate = tuple(
            mode for index, mode in enumerate(modes) if mode in modes[:index]
        )
        if duplicate:
            raise AgUiNativeStreamConfigurationError(
                f"AG-UI stream_mode contains duplicate modes: {duplicate!r}"
            )
        supported = frozenset((*_REQUIRED_MODES, *_SUPPORTED_EXTRA_MODES))
        unsupported = tuple(
            mode for mode in modes if not isinstance(mode, str) or mode not in supported
        )
        if unsupported:
            raise AgUiNativeStreamConfigurationError(
                f"AG-UI stream_mode contains unsupported modes: {unsupported!r}"
            )
        missing = tuple(mode for mode in _REQUIRED_MODES if mode not in modes)
        if missing:
            raise AgUiNativeStreamConfigurationError(
                f"AG-UI stream_mode is missing required modes: {missing!r}"
            )
        extra_modes = cast(
            tuple[StreamMode, ...],
            tuple(mode for mode in modes if mode not in _REQUIRED_MODES),
        )

    if "version" in forwarded and forwarded.pop("version") != "v2":
        raise AgUiNativeStreamConfigurationError("AG-UI native version must be 'v2'")
    if "subgraphs" in forwarded and forwarded.pop("subgraphs") is not True:
        raise AgUiNativeStreamConfigurationError("AG-UI native subgraphs must be True")

    def invocation() -> AsyncIterator[Mapping[str, object]]:
        return astream(
            *args,
            **forwarded,
            stream_mode=(*_REQUIRED_MODES, *extra_modes),
            version="v2",
            subgraphs=True,
        )

    return invocation


__all__ = ["_bind_agui_graph_astream", "_bind_graph_identity"]
