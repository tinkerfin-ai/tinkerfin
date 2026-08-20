"""Explicit LangGraph native invocation contract for strict AG-UI runs."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import cast

from langgraph.types import StreamMode

from tinkerfin_agui_adapter import Identity

_REQUIRED_MODES: tuple[StreamMode, ...] = ("messages", "tasks", "values")
_SUPPORTED_EXTRA_MODES: frozenset[StreamMode] = frozenset(
    {"updates", "checkpoints", "debug", "custom"}
)
_RESERVED_OPTIONS = frozenset({"stream_mode", "version", "subgraphs"})


class AgUiNativeStreamConfigurationError(ValueError):
    """Report an invalid native LangGraph stream contract for AG-UI conversion."""


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
        return options.get(name) if isinstance(options, Mapping) else None

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
        options = dict(raw_options) if isinstance(raw_options, Mapping) else {}
        options[name] = value
        bound.arguments[variable_keyword] = options

    raw_config = read("config")
    if raw_config is None:
        config: dict[str, object] = {}
    elif isinstance(raw_config, Mapping):
        config = dict(raw_config)
    else:
        raise TypeError("config must be a mapping or None")
    raw_configurable = config.get("configurable")
    if raw_configurable is None:
        configurable: dict[str, object] = {}
    elif isinstance(raw_configurable, Mapping):
        configurable = dict(raw_configurable)
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


@dataclass(frozen=True, slots=True)
class AgUiNativeStreamConfig:
    """Add optional modes to the fixed LangGraph stream required by AG-UI."""

    extra_modes: tuple[StreamMode, ...] = ()

    def bind(
        self,
        astream: Callable[..., AsyncIterator[Mapping[str, object]]],
        /,
        *args: object,
        **options: object,
    ) -> AgUiNativeStreamInvocation:
        """Bind one lazy native invocation to this exact configuration.

        Args:
            astream: Asynchronous iterator factory such as a compiled Graph's
                ``astream`` method.
            *args: Positional invocation arguments forwarded unchanged.
            **options: Non-reserved keyword arguments forwarded unchanged.

        Returns:
            A frozen zero-argument invocation for ``TinkerFin.run``.

        Raises:
            TypeError: ``astream`` is not callable.
        """

        if not callable(astream):
            raise TypeError("astream must be callable")
        return AgUiNativeStreamInvocation(
            config=self,
            _astream=astream,
            _args=args,
            _options=tuple(options.items()),
        )

    def _validate(self) -> None:
        modes = self.extra_modes
        if not isinstance(modes, tuple):
            raise AgUiNativeStreamConfigurationError(
                "AG-UI native extra_modes must be a tuple"
            )
        required = tuple(mode for mode in modes if mode in _REQUIRED_MODES)
        if required:
            raise AgUiNativeStreamConfigurationError(
                f"AG-UI native extra_modes cannot include required modes: {required!r}"
            )
        unsupported = tuple(
            mode
            for mode in modes
            if not isinstance(mode, str) or mode not in _SUPPORTED_EXTRA_MODES
        )
        if unsupported:
            raise AgUiNativeStreamConfigurationError(
                f"AG-UI native extra_modes contains unsupported modes: {unsupported!r}"
            )
        duplicate = tuple(
            mode for index, mode in enumerate(modes) if mode in modes[:index]
        )
        if duplicate:
            raise AgUiNativeStreamConfigurationError(
                f"AG-UI native extra_modes contains duplicate modes: {duplicate!r}"
            )


@dataclass(frozen=True, slots=True)
class AgUiNativeStreamInvocation:
    """Hold one lazy Graph invocation governed by an immutable AG-UI config."""

    config: AgUiNativeStreamConfig
    _astream: Callable[..., AsyncIterator[Mapping[str, object]]] = field(repr=False)
    _args: tuple[object, ...] = field(repr=False)
    _options: tuple[tuple[str, object], ...] = field(repr=False)

    def __call__(self) -> AsyncIterator[Mapping[str, object]]:
        """Create the configured native iterator without pulling its first part."""

        options = dict(self._options)
        return self._astream(
            *self._args,
            **options,
            stream_mode=(*_REQUIRED_MODES, *self.config.extra_modes),
            version="v2",
            subgraphs=True,
        )

    def _validate(self) -> None:
        self.config._validate()
        conflicts = tuple(
            name for name, _ in self._options if name in _RESERVED_OPTIONS
        )
        if conflicts:
            raise AgUiNativeStreamConfigurationError(
                "AG-UI native invocation options cannot override "
                f"stream configuration: {conflicts!r}"
            )

    def _bind_identity(self, identity: Identity) -> AgUiNativeStreamInvocation:
        """Return a preflighted invocation carrying the canonical Graph thread."""

        bound = _bind_graph_identity(
            inspect.signature(self._astream),
            self._args,
            dict(self._options),
            identity=identity,
            require_v2=False,
        )
        return replace(
            self,
            _args=bound.args,
            _options=tuple(bound.kwargs.items()),
        )


def _bind_agui_graph_astream(
    astream: Callable[..., AsyncIterator[Mapping[str, object]]],
    /,
    *args: object,
    **options: object,
) -> AgUiNativeStreamInvocation:
    """校验原生 astream 保留参数并创建严格的 AG-UI 调用"""

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
            modes = tuple(raw_modes)
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
    invocation = AgUiNativeStreamConfig(extra_modes=extra_modes).bind(
        astream,
        *args,
        **forwarded,
    )
    invocation._validate()
    return invocation


__all__ = [
    "AgUiNativeStreamConfig",
    "AgUiNativeStreamConfigurationError",
    "AgUiNativeStreamInvocation",
]
