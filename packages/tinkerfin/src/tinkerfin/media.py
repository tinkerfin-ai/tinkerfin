"""Resolve persistent attachments only for the current model invocation."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from tinkerfin_contracts.media import Attachment, attachment_from_block


@dataclass(frozen=True, slots=True)
class AttachmentImage:
    """A bounded image variant whose bytes are borrowed for one model request."""

    data: bytes
    mime_type: str


_MessageT = TypeVar("_MessageT", bound=BaseMessage)


class AttachmentSupport:
    """Keep checkpoints durable while presenting supported images to a model.

    The host authorizes each attachment in ``read_image`` and owns its storage.
    This support borrows that resolver and creates no background tasks. Images
    from tool results are appended as a user message after the complete tool batch,
    preserving the assistant/tool pairing expected by chat providers. Documents
    remain references for the host's document-reading tools. Cancellation propagates.

    Automatic integration requires the built-in native Deep Agents factory; decorated
    or replaced factories are rejected before resource preparation. Independently
    constructed agents may install ``middleware()`` explicitly at their final model
    boundary, after model routing and before the provider invocation.
    """

    def __init__(
        self,
        *,
        read_image: Callable[[Attachment], Awaitable[AttachmentImage]],
        supports_images: Callable[[BaseChatModel], bool] | None = None,
        max_images: int = 5,
    ) -> None:
        """Configure request-time image access without acquiring host resources.

        Args:
            read_image: Authorize and read a size-limited image variant. Raise
                FileNotFoundError for unavailable attachments; other errors propagate.
            supports_images: Optional capability check for the actual request model.
                By default only an explicit ``image_inputs=True`` model profile
                enables images. This check never grants attachment access.
            max_images: Maximum distinct recent images resolved per invocation.

        Raises:
            TypeError: max_images is not an integer.
            ValueError: max_images is not positive.
        """
        if not callable(read_image):
            raise TypeError("read_image must be callable")
        if supports_images is not None and not callable(supports_images):
            raise TypeError("supports_images must be callable or None")
        if isinstance(max_images, bool) or not isinstance(max_images, int):
            raise TypeError("max_images must be an integer")
        if max_images < 1:
            raise ValueError("max_images must be positive")
        self._reference_token: ContextVar[str | None] = ContextVar(
            "attachment_reference_token", default=None
        )
        self._max_images = max_images
        self._read_image = read_image
        self._supports_images = supports_images

    def middleware(self) -> AgentMiddleware:
        """Protect model calls in an independently constructed async agent.

        Install this after model-routing middleware inside custom compiled agents.
        Registering a custom
        graph as a subagent does not grant it the parent's attachment resolver.
        """
        return _AttachmentMiddleware(self)

    async def _prepare_messages(
        self, source: Sequence[_MessageT], *, model: BaseChatModel
    ) -> list[_MessageT | HumanMessage]:
        """Return request-only content for an explicit direct model invocation.

        Args:
            source: Durable messages whose attachment references remain unchanged.
            model: Actual destination model; capability is evaluated for each call.

        Returns:
            Message copies containing authorized images or explanatory text.

        Raises:
            TypeError: The capability check returns anything other than a boolean.
            Exception: Authorization or storage failures other than missing files.
                Cancellation propagates without changing source messages.
        """
        token = self._reference_token.get()
        if token is not None:
            from ._attachment_agents import _reference_messages

            source = _reference_messages(source, shield=False, token=token)
        supports_images = (
            self._supports_images(model)
            if self._supports_images is not None
            else (model.profile or {}).get("image_inputs") is True
        )
        if not isinstance(supports_images, bool):
            raise TypeError("supports_images must return a boolean")
        recent: list[str] = []
        for message in source:
            if isinstance(message.content, list):
                for block in message.content:
                    attachment = attachment_from_block(block)
                    if attachment is not None and attachment.kind == "image":
                        if attachment.id in recent:
                            recent.remove(attachment.id)
                        recent.append(attachment.id)
        selected = set(recent[-self._max_images :])
        messages: list[_MessageT | HumanMessage] = []
        tool_images: list[str | dict[str, Any]] = []
        cache: dict[str, AttachmentImage] = {}
        for message in source:
            if not isinstance(message.content, list):
                messages.append(message)
                continue
            blocks: list[str | dict[str, Any]] = []
            for block in message.content:
                attachment = attachment_from_block(block)
                if attachment is None:
                    blocks.append(block)
                    continue
                caption = f"Attachment: {attachment.name}; id={attachment.id}"
                if attachment.kind != "image":
                    blocks.append(
                        {
                            "type": "text",
                            "text": caption
                            + ". Use the available document-reading tools to read its contents.",
                        }
                    )
                    continue
                if not supports_images:
                    blocks.append(
                        {
                            "type": "text",
                            "text": caption
                            + ". Image omitted: the current model cannot view images.",
                        }
                    )
                    continue
                if attachment.id not in selected or attachment.id in cache:
                    blocks.append(
                        {
                            "type": "text",
                            "text": caption
                            + ". Image remains stored; use the available image-reading tools to load it if needed.",
                        }
                    )
                    continue
                if attachment.id not in cache:
                    try:
                        cache[attachment.id] = await self._read_image(attachment)
                    except FileNotFoundError:
                        blocks.append(
                            {
                                "type": "text",
                                "text": caption
                                + ". Original image is unavailable; ask the user to upload it again before analyzing it.",
                            }
                        )
                        continue
                resolved = cache[attachment.id]
                image: dict[str, Any] = {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{resolved.mime_type};base64,{base64.b64encode(resolved.data).decode('ascii')}"
                    },
                    "extras": {"attachment": attachment.model_dump(mode="json")},
                }
                blocks.append({"type": "text", "text": caption})
                if isinstance(message, ToolMessage):
                    tool_images.extend([{"type": "text", "text": caption}, image])
                else:
                    blocks.append(image)
            messages.append(message.model_copy(update={"content": blocks}))
        if tool_images:
            messages.append(HumanMessage(content=tool_images))
        return messages


class _AttachmentMiddleware(AgentMiddleware):
    """Project attachments at the final destination model boundary."""

    def __init__(self, support: AttachmentSupport) -> None:
        self._support = support

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(
            request.override(
                messages=await self._support._prepare_messages(
                    request.messages, model=request.model
                )
            )
        )


__all__ = ["Attachment", "AttachmentImage", "AttachmentSupport"]
