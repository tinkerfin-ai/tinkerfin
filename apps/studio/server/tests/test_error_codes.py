"""业务错误码的整数身份与响应元数据契约"""

from tinkerfin_studio.api.errors import (
    AttachmentErrorCode,
    AuthErrorCode,
    ConversationErrorCode,
    ErrorCode,
    GlobalErrorCode,
    ModelErrorCode,
)


def test_all_error_codes_keep_integer_values_and_response_metadata() -> None:
    """全部错误码都必须是唯一整数并携带可公开响应元数据"""

    members: tuple[ErrorCode, ...] = (
        *GlobalErrorCode,
        *AuthErrorCode,
        *AttachmentErrorCode,
        *ModelErrorCode,
        *ConversationErrorCode,
    )

    assert len(members) == 49
    assert len({int(member) for member in members}) == len(members)
    assert all(type(member.value) is int for member in members)
    assert all(400 <= member.http_status <= 599 for member in members)
    assert all(member.message for member in members)
    assert (
        int(ModelErrorCode.NOT_FOUND),
        ModelErrorCode.NOT_FOUND.http_status,
        ModelErrorCode.NOT_FOUND.message,
    ) == (1_001_005_000, 422, "模型不存在")
