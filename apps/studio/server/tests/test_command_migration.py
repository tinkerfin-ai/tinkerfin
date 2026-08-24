"""forwardedProps command 数据迁移测试"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tinkerfin_studio.conversation.command_migration import (
    migrate_forwarded_commands,
    migrate_run_input,
    migrate_run_started_event,
)
from tinkerfin_studio.conversation.models import ConversationEvent, ConversationRun
from tinkerfin_studio.infrastructure.database import Base


def test_command_migration_transforms_only_the_removed_mode_field() -> None:
    """迁移必须保留模型和其他扩展字段"""

    run, changed = migrate_run_input(
        {
            "forwardedProps": {
                "model": "main",
                "mode": "plan",
                "trace": {"sampled": True},
            }
        }
    )
    event, event_changed = migrate_run_started_event(
        {
            "type": "RUN_STARTED",
            "input": {"forwardedProps": {"model": "main", "mode": "default"}},
        }
    )

    assert changed is True
    assert run == {
        "forwardedProps": {
            "model": "main",
            "command": {"plan": "on"},
            "trace": {"sampled": True},
        }
    }
    assert event_changed is True
    assert event["input"] == {
        "forwardedProps": {"model": "main", "command": {"plan": "off"}}
    }


@pytest.mark.parametrize(
    "forwarded_props",
    (
        {"mode": "custom"},
        {"mode": "plan", "command": {"plan": "on"}},
    ),
)
def test_command_migration_rejects_ambiguous_inputs(
    forwarded_props: dict[str, object],
) -> None:
    """未知或双写数据必须阻止整个迁移"""

    with pytest.raises(ValueError):
        migrate_run_input({"forwardedProps": forwarded_props})


@pytest.mark.asyncio
async def test_command_migration_dry_run_and_apply_are_consistent() -> None:
    """dry-run 不写入，apply 同步更新 run 与事件双份 JSON"""

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(tzinfo=None)
    run_input = {
        "messages": [],
        "forwardedProps": {"model": "main", "mode": "plan"},
    }
    event_json = {
        "type": "RUN_STARTED",
        "threadId": "thread-1",
        "runId": "run-1",
        "input": {"forwardedProps": {"model": "main", "mode": "default"}},
    }
    async with sessions() as session:
        run = ConversationRun(
            conversation_thread_id=1,
            run_id="run-1",
            agent_type="main",
            status="success",
            input_json=run_input,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        event = ConversationEvent(
            conversation_thread_id=1,
            run_id="run-1",
            seq=1,
            event_id="event-1",
            event_type="RUN_STARTED",
            schema_version=3,
            protocol_version="ag-ui-protocol@0.1.19",
            event_json=event_json,
            event_text=json.dumps(
                event_json,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            created_at=now,
        )
        session.add_all([run, event])
        await session.commit()

        dry_run = await migrate_forwarded_commands(session, apply=False)
        assert (dry_run.runs, dry_run.events, dry_run.active_runs) == (1, 1, 0)
        assert run.input_json == run_input
        assert event.event_json == event_json

        applied = await migrate_forwarded_commands(session, apply=True)
        await session.commit()
        assert (applied.runs, applied.events, applied.active_runs) == (1, 1, 0)
        assert run.input_json == {
            "messages": [],
            "forwardedProps": {
                "model": "main",
                "command": {"plan": "on"},
            },
        }
        assert event.event_json["input"] == {
            "forwardedProps": {
                "model": "main",
                "command": {"plan": "off"},
            }
        }
        assert json.loads(event.event_text) == event.event_json

    await engine.dispose()
