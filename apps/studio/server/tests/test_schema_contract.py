from sqlalchemy import MetaData

from tinkerfin_studio.auth.models import User
from tinkerfin_studio.conversation.models import (
    ConversationEvent,
    ConversationInterrupt,
    ConversationMessage,
    ConversationRun,
    ConversationThread,
)
from tinkerfin_studio.infrastructure.database import Base
from tinkerfin_studio.models.entity import AgentModel


def test_business_schema_contains_no_foreign_keys() -> None:
    """Studio 表间引用完整性必须由应用层维护"""

    registered = (
        User,
        AgentModel,
        ConversationThread,
        ConversationRun,
        ConversationEvent,
        ConversationInterrupt,
        ConversationMessage,
    )
    assert {model.__tablename__ for model in registered} == set(Base.metadata.tables)
    assert Base.metadata.tables
    assert all(not table.foreign_keys for table in Base.metadata.tables.values())


def test_business_schema_exposes_the_complete_current_table_set() -> None:
    """全量建表脚本的业务表集合应由当前 ORM 明确定义"""

    metadata: MetaData = Base.metadata

    assert set(metadata.tables) == {
        "users",
        "agent_models",
        "conversation_threads",
        "conversation_runs",
        "conversation_events",
        "conversation_interrupts",
        "conversation_messages",
    }
