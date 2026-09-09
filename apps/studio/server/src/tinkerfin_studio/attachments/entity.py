"""Studio 附件的归属和存储记录"""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from tinkerfin_studio.infrastructure.database import Base


class AttachmentFile(Base):
    """记录附件原件、使用归属和可访问状态"""

    __tablename__ = "conversation_attachments"
    __table_args__ = (
        Index("ix_conversation_attachments_owner", "user_id", "thread_id"),
        Index("ix_conversation_attachments_cleanup", "thread_id", "created_at"),
        {"comment": "会话上传和生成附件的持久化引用"},
    )
    id: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        comment="服务端随机附件 ID，同时作为不透明存储标识",
    )
    user_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="附件所属用户 ID"
    )
    thread_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="附件绑定的会话 ID；空值表示未发送草稿"
    )
    message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="首次使用或生成附件的权威消息 ID"
    )
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="用户可见文件名，不用于存储路径"
    )
    mime_type: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="经服务端验证的文件媒体类型"
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="原件大小，单位为字节"
    )
    sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="原件完整内容的 SHA-256 校验值"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="uploading",
        comment="uploading、ready 或 deleting；仅 ready 可用",
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="user",
        comment="user 表示用户上传，tool 表示工具生成",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
        comment="UTC 创建时间，用于未发送附件的清理",
    )
