from sqlalchemy import MetaData

from tinkerfin_studio.auth.models import User
from tinkerfin_studio.conversation.models import (
    ConversationInterruptClaim,
    ConversationRunRegistration,
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
        ConversationRunRegistration,
        ConversationInterruptClaim,
    )
    assert {model.__tablename__ for model in registered} == set(Base.metadata.tables)
    assert Base.metadata.tables
    assert all(not table.foreign_keys for table in Base.metadata.tables.values())


def test_business_schema_exposes_only_current_studio_tables() -> None:
    """业务 ORM 不复制 AG-UI 正文或框架 Trace 表"""

    metadata: MetaData = Base.metadata

    assert set(metadata.tables) == {
        "users",
        "agent_models",
        "conversation_threads",
        "conversation_run_registrations",
        "conversation_interrupt_claims",
    }
