"""Attachment access composed with the locked Deep Agents filesystem boundary."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from functools import wraps
from importlib.metadata import version
from types import FunctionType
from typing import Any, TypeVar, cast
from uuid import uuid4

from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.graph import get_default_model
from deepagents.middleware import subagents as native_subagents
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from deepagents.profiles.harness.harness_profiles import (
    HarnessProfile,
    _get_harness_profile,
    _harness_profile_for_model,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from tinkerfin_contracts.media import Attachment, attachment_from_block

from .errors import TinkerFinLifecycleError
from .media import AttachmentSupport

_MessageT = TypeVar("_MessageT", bound=BaseMessage)


def _reference_messages(
    messages: Sequence[_MessageT], *, shield: bool, token: str
) -> list[_MessageT]:
    """Keep references outside native media scrubbing and text eviction payloads."""
    result = []
    for message in messages:
        if isinstance(message.content, str):
            result.append(message)
            continue
        blocks = []
        for block in message.content:
            if not shield and isinstance(block, dict):
                extras = block.get("extras")
                if (
                    isinstance(extras, dict)
                    and extras.get("tinkerfin_attachment_caption") == token
                ):
                    continue
            attachment = attachment_from_block(block)
            if (
                not shield
                and isinstance(block, dict)
                and block.get("type") == "non_standard"
            ):
                value = block.get("value")
                if (
                    isinstance(value, dict)
                    and value.get("token") == token
                    and "tinkerfin_attachment" in value
                ):
                    attachment = Attachment.model_validate(
                        value["tinkerfin_attachment"]
                    )
            if attachment is None:
                blocks.append(block)
            elif shield:
                blocks.append(
                    {
                        "type": "text",
                        "text": f"Attachment: {attachment.name}; id={attachment.id}; mime_type={attachment.mime_type}",
                        "extras": {"tinkerfin_attachment_caption": token},
                    }
                )
                blocks.append(
                    {
                        "type": "non_standard",
                        "value": {
                            "token": token,
                            "tinkerfin_attachment": attachment.model_dump(mode="json"),
                        },
                    }
                )
            else:
                blocks.append(attachment.content_block())
        result.append(message.model_copy(update={"content": blocks}))
    return result


class _AttachmentFilesystem(AgentMiddleware):
    """Borrow all native filesystem behavior while owning attachment model access.

    Deep Agents 0.7.5 inherits named filesystem replacements in its automatic
    general-purpose agent. Keeping that genuine filesystem slot preserves the
    native GP profile, dynamic response schema, permissions and tool construction.
    Filesystem eviction may tag request copies in a Command; restore durable
    descriptors before the command can reach checkpoint state.
    """

    def __init__(
        self, filesystem: AgentMiddleware[Any, Any, Any], support: AttachmentSupport
    ) -> None:
        self._filesystem = filesystem
        self._support = support
        self.state_schema = filesystem.state_schema
        self.tools = filesystem.tools
        self.transformers = filesystem.transformers

    @property
    def name(self) -> str:
        return self._filesystem.name

    def wrap_model_call(self, request, handler):
        raise NotImplementedError("Attachment access requires async model invocation")

    async def awrap_model_call(
        self, request: ModelRequest, handler
    ) -> ModelResponse | ExtendedModelResponse | AIMessage:
        token = uuid4().hex

        binding = self._support._reference_token.set(token)
        try:
            result = await self._filesystem.awrap_model_call(
                request.override(
                    messages=_reference_messages(
                        request.messages, shield=True, token=token
                    )
                ),
                handler,
            )
        finally:
            self._support._reference_token.reset(binding)
        if isinstance(result, ExtendedModelResponse) and result.command is not None:
            update = result.command.update
            if isinstance(update, Mapping) and isinstance(update.get("messages"), list):
                restored = _reference_messages(
                    update["messages"], shield=False, token=token
                )
                result = replace(
                    result,
                    command=replace(
                        result.command, update={**update, "messages": restored}
                    ),
                )
        return result


def attachment_filesystem(
    filesystem: AgentMiddleware[Any, Any, Any], support: AttachmentSupport
) -> AgentMiddleware[Any, Any, Any]:
    """Decorate only hooks present on the borrowed filesystem middleware.

    LangChain detects hooks on the class when constructing nodes. Delegating
    only existing overrides preserves the graph topology and custom hook metadata;
    registering no-op before/after hooks would introduce additional checkpoints.
    """
    hooks: dict[str, object] = {}
    for name in (
        "before_agent",
        "abefore_agent",
        "before_model",
        "abefore_model",
        "after_model",
        "aafter_model",
        "after_agent",
        "aafter_agent",
        "wrap_tool_call",
        "awrap_tool_call",
    ):
        if getattr(type(filesystem), name) is getattr(AgentMiddleware, name):
            continue
        original = getattr(filesystem, name)

        # The bound original retains ownership of configuration, tools and state.
        def delegate(method):
            if inspect.iscoroutinefunction(method):

                @wraps(method)
                async def async_hook(self, *args, **kwargs):
                    return await method(*args, **kwargs)

                return async_hook

            @wraps(method)
            def sync_hook(self, *args, **kwargs):
                return method(*args, **kwargs)

            return sync_hook

        hooks[name] = delegate(original)
    decorated = type("_AttachmentFilesystem", (_AttachmentFilesystem,), hooks)
    return decorated(filesystem, support)


def _bind_dependencies(
    original: Callable[..., Any], **dependencies: object
) -> Callable[..., Any]:
    """Bind reviewed upstream dependencies without touching module globals.

    The callable retains its original code, closure, defaults and signature. The
    isolated globals dictionary replaces only named construction dependencies.
    Deep Agents exposes no per-agent terminal middleware hook for the automatic
    GP or for dynamic-schema recompilation; binding those construction calls
    keeps its own assembly and routing implementation authoritative.
    """
    source = original
    if not isinstance(source, FunctionType):
        raise TinkerFinLifecycleError(
            "Attachment access requires a Python native factory"
        )
    if hasattr(source, "__wrapped__"):
        raise TinkerFinLifecycleError(
            "Automatic attachment access requires the unmodified native Deep Agents factory"
        )
    if not dependencies.keys() <= source.__globals__.keys():
        raise TinkerFinLifecycleError(
            "Attachment access requires the native Deep Agents construction contract"
        )
    configured = FunctionType(
        source.__code__,
        {**source.__globals__, **dependencies},
        source.__name__,
        source.__defaults__,
        source.__closure__,
    )
    configured.__kwdefaults__ = source.__kwdefaults__
    return wraps(source)(configured)


def _native_attachment_factory(
    factory: Callable[..., object], support: AttachmentSupport
) -> Callable[..., object]:
    """Install model projection after user and harness routing in every owned agent."""

    from .deep_agent import _public_create_agent_contract

    # Never clone or unwrap caller instrumentation: copying its globals could
    # hide side effects, while mutating them would break concurrent builds.
    source = factory
    if (
        not isinstance(source, FunctionType)
        or not isinstance(_public_create_agent_contract, FunctionType)
        or source.__code__ is not _public_create_agent_contract.__code__
        or source.__code__.co_name != "create_deep_agent"
        or hasattr(source, "__wrapped__")
    ):
        raise TinkerFinLifecycleError(
            "Automatic attachment access requires the unmodified native Deep Agents factory"
        )

    def final_projection(builder: Callable[..., object]) -> Callable[..., object]:
        @wraps(builder)
        def create_with_attachments(*args: object, **kwargs: Any) -> object:
            middleware = kwargs.get("middleware", ())
            kwargs["middleware"] = (*middleware, support.middleware())
            return builder(*args, **kwargs)

        return create_with_attachments

    subagent_factory = _bind_dependencies(
        native_subagents.create_sub_agent,
        create_agent=final_projection(
            native_subagents.create_sub_agent.__globals__["create_agent"]
        ),
    )
    task_factory = _bind_dependencies(
        native_subagents._build_task_tool, create_sub_agent=subagent_factory
    )
    subagent_init = _bind_dependencies(
        native_subagents.SubAgentMiddleware.__init__, _build_task_tool=task_factory
    )
    private_keys = native_subagents.SubAgentMiddleware.private_state_keys
    assert private_keys.fset is not None
    private_keys_setter = _bind_dependencies(
        private_keys.fset, _build_task_tool=task_factory
    )
    subagent_middleware = type(
        "SubAgentMiddleware",
        (native_subagents.SubAgentMiddleware,),
        {
            "__init__": subagent_init,
            "private_state_keys": private_keys.setter(private_keys_setter),
        },
    )
    return _bind_dependencies(
        factory,
        create_agent=final_projection(source.__globals__["create_agent"]),
        SubAgentMiddleware=subagent_middleware,
    )


def attachment_agent_factory(
    factory: Callable[..., object],
    signature: inspect.Signature,
    support: AttachmentSupport,
) -> Callable[..., object]:
    """Install complete attachment-aware filesystem stacks at native build time."""

    @wraps(factory)
    def build(*args: object, **kwargs: object) -> object:
        installed = {
            package: version(package) for package in ("deepagents", "langchain")
        }
        if installed != {"deepagents": "0.7.5", "langchain": "1.3.14"}:
            raise TinkerFinLifecycleError(
                "Attachment access requires the locked Deep Agents middleware contract",
                context=installed,
            )
        native_factory = _native_attachment_factory(factory, support)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        raw_model = arguments.get("model")
        model = (
            get_default_model()
            if raw_model is None
            else cast(str | BaseChatModel, raw_model)
        )
        if raw_model is None:
            arguments["model"] = model
        backend = cast(BackendProtocol, arguments.get("backend") or StateBackend())
        arguments["backend"] = backend

        def middleware_for(
            values: object, destination: str | BaseChatModel, permissions: object
        ) -> tuple[AgentMiddleware[Any, Any, Any], ...]:
            middleware = list(cast(Sequence[AgentMiddleware[Any, Any, Any]], values))
            existing = next(
                (item for item in middleware if item.name == "FilesystemMiddleware"),
                None,
            )
            if existing is None:
                # Query the same profile without constructing a second provider
                # client. Native graph construction resolves string models once.
                profile = (
                    _get_harness_profile(destination) or HarnessProfile()
                    if isinstance(destination, str)
                    else _harness_profile_for_model(destination, None)
                )
                existing = FilesystemMiddleware(
                    backend=backend,
                    custom_tool_descriptions=profile.tool_description_overrides,
                    _permissions=cast(list[FilesystemPermission] | None, permissions),
                )
                middleware.insert(0, existing)
            return tuple(
                attachment_filesystem(item, support) if item is existing else item
                for item in middleware
            )

        arguments["middleware"] = middleware_for(
            arguments.get("middleware", ()),
            cast(str | BaseChatModel, raw_model or model),
            arguments.get("permissions"),
        )
        subagents = []
        for original in cast(
            Sequence[Mapping[str, object]], arguments.get("subagents") or ()
        ):
            spec = dict(original)
            if "runnable" not in spec and "graph_id" not in spec:
                spec["middleware"] = middleware_for(
                    spec.get("middleware", ()),
                    cast(str | BaseChatModel, spec.get("model", raw_model or model)),
                    spec.get("permissions", arguments.get("permissions")),
                )
            subagents.append(spec)
        arguments["subagents"] = subagents
        return native_factory(*bound.args, **bound.kwargs)

    return build
