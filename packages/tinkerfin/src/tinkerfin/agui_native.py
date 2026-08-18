"""Explicit LangGraph native invocation contract for strict AG-UI runs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field

from langgraph.types import StreamMode

_REQUIRED_MODES: tuple[StreamMode, ...] = ("messages", "tasks", "values")
_SUPPORTED_EXTRA_MODES: frozenset[StreamMode] = frozenset(
    {"updates", "checkpoints", "debug", "custom"}
)
_RESERVED_OPTIONS = frozenset({"stream_mode", "version", "subgraphs"})


class AgUiNativeStreamConfigurationError(ValueError):
    """Report an invalid native LangGraph stream contract for AG-UI conversion."""


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


__all__ = [
    "AgUiNativeStreamConfig",
    "AgUiNativeStreamConfigurationError",
    "AgUiNativeStreamInvocation",
]
