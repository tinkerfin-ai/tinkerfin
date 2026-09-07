"""Attachment projection preserves durable content and provider tool pairing."""

import pytest
from ag_ui.core import UserMessage
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import JsonValue, TypeAdapter

from tinkerfin.media import Attachment, AttachmentImage, AttachmentSupport
from tinkerfin_agui_adapter.media import user_content_to_agui, user_message_to_langchain
from tinkerfin_tracing.redaction import RedactionContext, secure_redact

FILE = Attachment(id="sample", name="chart.png", mime_type="image/png", size_bytes=4)


async def _capture_response(request):
    return ModelResponse(result=request.messages)


def test_agui_attachment_roundtrip():
    original = HumanMessage(
        content=[{"type": "text", "text": "read"}, FILE.content_block()], id="m"
    )
    wire = UserMessage.model_validate(
        {"id": "m", "content": user_content_to_agui(original.content)}
    )
    assert user_message_to_langchain(wire).content == original.content


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_images", [True, False])
async def test_model_projection_preserves_original_and_tool_order(supports_images):
    original = [
        AIMessage(content="", tool_calls=[{"id": "t", "name": "chart", "args": {}}]),
        ToolMessage(content=[FILE.content_block()], tool_call_id="t"),
    ]
    reads = []

    async def read_image(attachment):
        reads.append(attachment.id)
        return AttachmentImage(data=b"test", mime_type="image/png")

    projected = []

    async def handler(request):
        projected.extend(request.messages)
        return ModelResponse(result=[AIMessage(content="done")])

    request = ModelRequest(
        model=FakeListChatModel(responses=["done"]), messages=original, tools=[]
    )
    await (
        AttachmentSupport(
            read_image=read_image, supports_images=lambda model: supports_images
        )
        .middleware()
        .awrap_model_call(request, handler)
    )
    assert original[1].content == [FILE.content_block()]
    assert isinstance(projected[1], ToolMessage)
    assert len(reads) == int(supports_images)
    if supports_images:
        assert isinstance(projected[-1], HumanMessage)
        safe = secure_redact(
            TypeAdapter(JsonValue).validate_python(projected[-1].content),
            context=RedactionContext(content_kind="model_request"),
            redactor=None,
        )
        assert isinstance(safe, list)
        assert safe[-1] == FILE.content_block()
        assert "dGVzdA==" not in str(safe)
    else:
        assert "cannot view images" in str(projected[1].content)


@pytest.mark.asyncio
async def test_missing_history_image_is_explicit_and_does_not_mutate_history():
    """An unavailable file must not be represented as an image the model saw."""

    async def missing(attachment):
        raise FileNotFoundError(attachment.id)

    message = HumanMessage(content=[FILE.content_block()])
    projected = (
        await AttachmentSupport(read_image=missing, supports_images=lambda model: True)
        .middleware()
        .awrap_model_call(
            ModelRequest(
                model=FakeListChatModel(responses=["done"]),
                messages=[message],
                tools=[],
            ),
            _capture_response,
        )
    )
    assert isinstance(projected, ModelResponse)
    projected = projected.result
    assert "unavailable" in str(projected[0].content)
    assert message.content == [FILE.content_block()]


@pytest.mark.asyncio
async def test_real_tool_return_keeps_attachment_as_a_content_block():
    """Real tool invocation preserves structured attachments for downstream adapters."""
    from langchain_core.tools import tool

    from tinkerfin_contracts.media import attachment_from_block

    @tool
    async def picture():
        """Return a stored picture."""
        return [FILE.content_block()]

    result = await picture.ainvoke(
        {"type": "tool_call", "id": "call", "name": "picture", "args": {}}
    )
    assert isinstance(result, ToolMessage)
    assert isinstance(result.content, list)
    assert attachment_from_block(result.content[0]) == FILE


def test_invalid_request_image_metadata_uses_safe_trace_error():
    from tinkerfin_tracing import TraceCaptureRejected

    with pytest.raises(TraceCaptureRejected) as rejected:
        secure_redact(
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,private"},
                "extras": {"attachment": {"id": "private"}},
            },
            context=RedactionContext(content_kind="model_request"),
            redactor=None,
        )
    assert "private" not in str(rejected.value)
    assert rejected.value.__cause__ is not None


@pytest.mark.asyncio
async def test_image_resolution_cancellation_preserves_input_messages():
    import asyncio

    async def read_image(attachment):
        raise asyncio.CancelledError()

    original = HumanMessage(content=[FILE.content_block()])
    with pytest.raises(asyncio.CancelledError):
        await (
            AttachmentSupport(read_image=read_image, supports_images=lambda model: True)
            .middleware()
            .awrap_model_call(
                ModelRequest(
                    model=FakeListChatModel(responses=["done"]),
                    messages=[original],
                    tools=[],
                ),
                _capture_response,
            )
        )
    assert original.content == [FILE.content_block()]


@pytest.mark.asyncio
async def test_capabilities_follow_actual_model_and_authorization_is_independent():
    vision = FakeListChatModel(responses=["done"], profile={"image_inputs": True})
    text = FakeListChatModel(responses=["done"], profile={"image_inputs": False})
    unknown = FakeListChatModel(responses=["done"])
    reads = []

    async def denied(attachment):
        reads.append(attachment.id)
        raise PermissionError("attachment is not authorized")

    middleware = AttachmentSupport(read_image=denied).middleware()
    original = HumanMessage(content=[FILE.content_block()])
    for model in [text, unknown]:
        result = await middleware.awrap_model_call(
            ModelRequest(model=model, messages=[original], tools=[]), _capture_response
        )
        assert isinstance(result, ModelResponse)
        assert "cannot view images" in str(result.result[0].content)
    assert not reads
    with pytest.raises(PermissionError, match="not authorized"):
        await middleware.awrap_model_call(
            ModelRequest(model=vision, messages=[original], tools=[]), _capture_response
        )
    assert reads == [FILE.id]
    assert original.content == [FILE.content_block()]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", ["yes", 1, None])
async def test_host_model_capability_override_rejects_non_boolean_results(
    invalid_value,
):
    async def read_image(attachment):
        pytest.fail("invalid capability must not resolve images")

    support = AttachmentSupport(
        read_image=read_image, supports_images=lambda model: invalid_value
    )
    with pytest.raises(TypeError, match="return a boolean"):
        await support.middleware().awrap_model_call(
            ModelRequest(
                model=FakeListChatModel(responses=["done"]), messages=[], tools=[]
            ),
            _capture_response,
        )
