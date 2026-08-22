from __future__ import annotations

import asyncio
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest
from ag_ui.core import BaseEvent, Event
from langgraph.store.mysql.asyncmy import AsyncMyStore
from pydantic import TypeAdapter
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from tinkerfin import Identity
from tinkerfin_messaging.models import MessageEnvelope
from tinkerfin_sandbox import get_sqlalchemy_opensandbox_state_schema
from tinkerfin_studio.auth.models import User
from tinkerfin_studio.conversation.models import (
    ConversationEvent,
    ConversationInterrupt,
    ConversationMessage,
    ConversationRun,
    ConversationThread,
)
from tinkerfin_studio.conversation.projection import ConversationProjector
from tinkerfin_studio.conversation.repository import ConversationRepository
from tinkerfin_studio.infrastructure.database import Base
from tinkerfin_studio.models.entity import AgentModel

_TEST_URL_ENV = "TINKERFIN_STUDIO_TEST_MYSQL_URL"
_SCHEMA_PATH = Path(__file__).parents[1] / "database" / "mysql" / "schema.sql"
_DATABASE_NAME_PATTERN = re.compile(r"\Atinkerfin_schema_[a-f0-9]{16}_(sql|runtime)\Z")
_EXPECTED_TABLES = frozenset(
    {
        "users",
        "agent_models",
        "conversation_threads",
        "conversation_runs",
        "conversation_events",
        "conversation_interrupts",
        "conversation_messages",
        "store_migrations",
        "store",
        "tinkerfin_opensandbox_schema_versions",
        "tinkerfin_opensandbox_owners",
        "tinkerfin_opensandbox_workers",
        "tinkerfin_opensandbox_warm_slots",
        "tinkerfin_opensandbox_cleanup",
    }
)
_BUSINESS_MODELS = (
    User,
    AgentModel,
    ConversationThread,
    ConversationRun,
    ConversationEvent,
    ConversationInterrupt,
    ConversationMessage,
)
_EVENT_ADAPTER = TypeAdapter(Event)


def _projection_event(
    seq: int,
    value: dict[str, object],
) -> tuple[MessageEnvelope, BaseEvent]:
    event = cast(BaseEvent, _EVENT_ADAPTER.validate_python(value))
    return (
        MessageEnvelope(
            channel="studio-conversation-agui",
            identity=Identity(
                threadId="users/1/threads/thread-projection-lock",
                runId="run-projection-lock",
            ),
            seq=seq,
            message_id=f"run-projection-lock:{seq}",
            codec="agui.event.v1",
            payload=event.model_dump_json(by_alias=True, exclude_none=True).encode(),
            created_at=datetime(2026, 8, 22, 2, seq, tzinfo=UTC),
        ),
        event,
    )


class _ReflectedColumn(TypedDict):
    name: str
    type: object
    nullable: bool
    default: object | None
    autoincrement: NotRequired[bool]
    comment: NotRequired[str | None]


@dataclass(frozen=True, slots=True)
class _ColumnSignature:
    type_name: str
    length: int | None
    precision: int | None
    scale: int | None
    unsigned: bool
    nullable: bool
    default: str | None
    autoincrement: bool


@dataclass(frozen=True, slots=True)
class _TableSignature:
    columns: dict[str, _ColumnSignature]
    primary_key: tuple[str, ...]
    indexes: dict[str, tuple[tuple[str, ...], bool]]
    unique_constraints: dict[str, tuple[str, ...]]
    check_constraints: dict[str, str]
    foreign_keys: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class _SchemaReflection:
    tables: dict[str, _TableSignature]
    table_comments: dict[str, str]
    column_comments: dict[str, dict[str, str]]


def _configured_url() -> URL:
    value = os.environ.get(_TEST_URL_ENV)
    if value is None:
        pytest.skip(f"未配置专用 MySQL URL：{_TEST_URL_ENV}")
    url = make_url(value)
    if not url.drivername.startswith("mysql"):
        pytest.fail(f"{_TEST_URL_ENV} 必须使用 MySQL URL")
    if url.database is None:
        pytest.fail(f"{_TEST_URL_ENV} 必须指定可连接的管理数据库")
    return url.set(drivername="mysql+asyncmy")


def _database_url(admin_url: URL, database_name: str) -> URL:
    if _DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("临时数据库名称不符合安全约束")
    return admin_url.set(database=database_name, drivername="mysql+asyncmy")


def _ddl_statements(ddl: str) -> tuple[str, ...]:
    return tuple(statement.strip() for statement in ddl.split(";") if statement.strip())


def _normalize_default(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while len(normalized) >= 2 and normalized[0] == "(" and normalized[-1] == ")":
        normalized = normalized[1:-1].strip()
    if (
        len(normalized) >= 2
        and normalized[0] in {"'", '"'}
        and normalized[-1] == normalized[0]
    ):
        normalized = normalized[1:-1]
    return normalized.casefold()


def _column_signature(column: _ReflectedColumn) -> _ColumnSignature:
    column_type = column["type"]
    return _ColumnSignature(
        type_name=type(column_type).__name__.casefold(),
        length=cast(int | None, getattr(column_type, "length", None)),
        precision=cast(int | None, getattr(column_type, "precision", None)),
        scale=cast(int | None, getattr(column_type, "scale", None)),
        unsigned=bool(getattr(column_type, "unsigned", False)),
        nullable=bool(column["nullable"]),
        default=_normalize_default(column.get("default")),
        autoincrement=column.get("autoincrement") is True,
    )


def _normalize_check_sql(value: object) -> str:
    return re.sub(r"[\s`()]+", "", str(value)).casefold()


def _reflect_schema(connection: Connection) -> _SchemaReflection:
    inspector = inspect(connection)
    table_names = sorted(inspector.get_table_names())
    tables: dict[str, _TableSignature] = {}
    table_comments: dict[str, str] = {}
    column_comments: dict[str, dict[str, str]] = {}
    for table_name in table_names:
        columns = cast(list[_ReflectedColumn], inspector.get_columns(table_name))
        tables[table_name] = _TableSignature(
            columns={
                str(column["name"]): _column_signature(column) for column in columns
            },
            primary_key=tuple(
                cast(
                    list[str],
                    inspector.get_pk_constraint(table_name).get(
                        "constrained_columns", []
                    ),
                )
            ),
            indexes={
                str(index["name"]): (
                    tuple(cast(list[str], index.get("column_names", []))),
                    bool(index.get("unique", False)),
                )
                for index in inspector.get_indexes(table_name)
                if index.get("name") is not None
            },
            unique_constraints={
                str(constraint["name"]): tuple(
                    cast(list[str], constraint.get("column_names", []))
                )
                for constraint in inspector.get_unique_constraints(table_name)
                if constraint.get("name") is not None
            },
            check_constraints={
                str(constraint["name"]): _normalize_check_sql(
                    constraint.get("sqltext", "")
                )
                for constraint in inspector.get_check_constraints(table_name)
                if constraint.get("name") is not None
            },
            foreign_keys=tuple(
                tuple(
                    cast(
                        list[str],
                        foreign_key.get("constrained_columns", []),
                    )
                )
                for foreign_key in inspector.get_foreign_keys(table_name)
            ),
        )
        table_comments[table_name] = str(
            inspector.get_table_comment(table_name).get("text") or ""
        )
        column_comments[table_name] = {
            str(column["name"]): str(column.get("comment") or "") for column in columns
        }
    return _SchemaReflection(
        tables=tables,
        table_comments=table_comments,
        column_comments=column_comments,
    )


async def _execute_ddl(engine: AsyncEngine, ddl: str) -> None:
    async with engine.begin() as connection:
        for statement in _ddl_statements(ddl):
            await connection.exec_driver_sql(statement)


async def _create_runtime_schema(engine: AsyncEngine, database_url: URL) -> None:
    assert {model.__tablename__ for model in _BUSINESS_MODELS} == set(
        Base.metadata.tables
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    store_url = database_url.set(drivername="mysql").render_as_string(
        hide_password=False
    )
    async with AsyncMyStore.from_conn_string(store_url) as store:
        await store.setup()
    sandbox_schema = get_sqlalchemy_opensandbox_state_schema(dialect="mysql")
    await _execute_ddl(engine, sandbox_schema.ddl)


async def _create_database(admin_engine: AsyncEngine, database_name: str) -> None:
    if _DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("临时数据库名称不符合安全约束")
    async with admin_engine.begin() as connection:
        await connection.exec_driver_sql(
            f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4"
        )


async def _drop_database(admin_engine: AsyncEngine, database_name: str) -> None:
    if _DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("临时数据库名称不符合安全约束")
    async with admin_engine.begin() as connection:
        await connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database_name}`")


@pytest.mark.studio_mysql_integration
async def test_full_schema_sql_matches_runtime_generated_mysql_schema() -> None:
    """全量脚本必须与三个运行时 schema 来源逐项一致"""

    admin_url = _configured_url()
    token = secrets.token_hex(8)
    sql_database = f"tinkerfin_schema_{token}_sql"
    runtime_database = f"tinkerfin_schema_{token}_runtime"
    sql_url = _database_url(admin_url, sql_database)
    runtime_url = _database_url(admin_url, runtime_database)
    admin_engine = create_async_engine(admin_url)
    sql_engine: AsyncEngine | None = None
    runtime_engine: AsyncEngine | None = None
    created_databases: list[str] = []
    try:
        for database_name in (sql_database, runtime_database):
            await _create_database(admin_engine, database_name)
            created_databases.append(database_name)
        sql_engine = create_async_engine(sql_url)
        runtime_engine = create_async_engine(runtime_url)
        await _execute_ddl(sql_engine, _SCHEMA_PATH.read_text(encoding="utf-8"))
        await _create_runtime_schema(runtime_engine, runtime_url)

        async with sql_engine.connect() as connection:
            sql_schema = await connection.run_sync(_reflect_schema)
            sql_store_versions = tuple(
                await connection.scalars(
                    text("SELECT v FROM store_migrations ORDER BY v")
                )
            )
            sql_sandbox_version = await connection.scalar(
                text(
                    "SELECT version FROM tinkerfin_opensandbox_schema_versions "
                    "WHERE component = 'opensandbox-state'"
                )
            )
        async with runtime_engine.connect() as connection:
            runtime_schema = await connection.run_sync(_reflect_schema)
            runtime_store_versions = tuple(
                await connection.scalars(
                    text("SELECT v FROM store_migrations ORDER BY v")
                )
            )
            runtime_sandbox_version = await connection.scalar(
                text(
                    "SELECT version FROM tinkerfin_opensandbox_schema_versions "
                    "WHERE component = 'opensandbox-state'"
                )
            )

        assert set(sql_schema.tables) == _EXPECTED_TABLES
        assert set(runtime_schema.tables) == _EXPECTED_TABLES
        assert sql_schema.tables == runtime_schema.tables
        assert sql_schema.tables["conversation_threads"].check_constraints == {
            "ck_conversation_threads_snapshot_version": "snapshot_version=3"
        }
        assert sql_schema.tables["conversation_events"].check_constraints == {
            "ck_conversation_events_schema_version": "schema_version=3"
        }
        business_table_names = {model.__tablename__ for model in _BUSINESS_MODELS}
        assert {
            table_name: sql_schema.table_comments[table_name]
            for table_name in business_table_names
        } == {
            table_name: runtime_schema.table_comments[table_name]
            for table_name in business_table_names
        }
        assert {
            table_name: sql_schema.column_comments[table_name]
            for table_name in business_table_names
        } == {
            table_name: runtime_schema.column_comments[table_name]
            for table_name in business_table_names
        }
        assert all(not table.foreign_keys for table in sql_schema.tables.values())
        assert all(not table.foreign_keys for table in runtime_schema.tables.values())
        assert all(sql_schema.table_comments.values())
        missing_column_comments = sorted(
            f"{table_name}.{column_name}"
            for table_name, comments in sql_schema.column_comments.items()
            for column_name, comment in comments.items()
            if not comment
        )
        assert missing_column_comments == []
        assert sql_store_versions == (0, 1)
        assert runtime_store_versions == (0, 1)
        assert sql_sandbox_version == 2
        assert runtime_sandbox_version == 2
    finally:
        if sql_engine is not None:
            await sql_engine.dispose()
        if runtime_engine is not None:
            await runtime_engine.dispose()
        for database_name in reversed(created_databases):
            await _drop_database(admin_engine, database_name)
        await admin_engine.dispose()


@pytest.mark.studio_mysql_integration
async def test_concurrent_same_run_resume_claim_uses_current_mysql_row() -> None:
    """同 runId 并发认领必须在 MySQL 默认隔离级别下保持幂等"""

    admin_url = _configured_url()
    database_name = f"tinkerfin_schema_{secrets.token_hex(8)}_runtime"
    database_url = _database_url(admin_url, database_name)
    admin_engine = create_async_engine(admin_url)
    engine: AsyncEngine | None = None
    created = False
    try:
        await _create_database(admin_engine, database_name)
        created = True
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
        )
        async with sessions() as setup_session:
            repository = ConversationRepository(setup_session)
            thread = await repository.create_thread(
                user_id=1,
                thread_id="thread-same-run-claim",
                title="并发认领",
                model_id=None,
            )
            now = datetime.now(UTC).replace(tzinfo=None)
            setup_session.add(
                ConversationInterrupt(
                    conversation_thread_id=thread.id,
                    run_id="run-interrupted",
                    resolved_run_id=None,
                    interrupt_id="interrupt-same-run",
                    status="pending",
                    reason="tool_call",
                    message="确认操作",
                    request_json={"id": "interrupt-same-run"},
                    resume_json=None,
                    created_at=now,
                    resolved_at=None,
                    updated_at=now,
                )
            )
            await repository.commit()
            thread_pk = thread.id

        ready_count = 0
        ready_lock = asyncio.Lock()
        release = asyncio.Event()

        async def claim() -> str | None:
            nonlocal ready_count
            async with sessions() as session:
                repository = ConversationRepository(session)
                await repository.get_thread(
                    user_id=1,
                    thread_id="thread-same-run-claim",
                )
                async with ready_lock:
                    ready_count += 1
                    if ready_count == 2:
                        release.set()
                await release.wait()
                rows = await repository.claim_pending_interrupts(
                    thread_pk=thread_pk,
                    run_id="run-same",
                    interrupt_ids=frozenset({"interrupt-same-run"}),
                )
                owner = rows[0].resolved_run_id
                await repository.commit()
                return owner

        owners = await asyncio.gather(claim(), claim())

        assert owners == ["run-same", "run-same"]
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            await _drop_database(admin_engine, database_name)
        await admin_engine.dispose()


@pytest.mark.studio_mysql_integration
async def test_concurrent_projectors_serialize_on_the_mysql_thread_row() -> None:
    """MySQL 行锁必须让相邻事件按提交后的最新 snapshot 序号串行投影"""

    admin_url = _configured_url()
    database_name = f"tinkerfin_schema_{secrets.token_hex(8)}_runtime"
    database_url = _database_url(admin_url, database_name)
    admin_engine = create_async_engine(admin_url)
    engine: AsyncEngine | None = None
    second_task: asyncio.Task[None] | None = None
    created = False
    try:
        await _create_database(admin_engine, database_name)
        created = True
        engine = create_async_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(
            engine,
            expire_on_commit=False,
            autoflush=False,
        )
        async with sessions() as setup_session:
            repository = ConversationRepository(setup_session)
            thread = await repository.create_thread(
                user_id=1,
                thread_id="thread-projection-lock",
                title="投影行锁",
                model_id="main",
            )
            await repository.create_main_run(
                thread_id=thread.id,
                run_id="run-projection-lock",
                model_id="main",
                input_json={"messages": []},
                config_json={},
            )
            await repository.commit()
            thread_pk = thread.id

        first_envelope, first_event = _projection_event(
            1,
            {
                "type": "RUN_STARTED",
                "threadId": "thread-projection-lock",
                "runId": "run-projection-lock",
            },
        )
        second_envelope, second_event = _projection_event(
            2,
            {
                "type": "RUN_FINISHED",
                "threadId": "thread-projection-lock",
                "runId": "run-projection-lock",
                "outcome": {"type": "success"},
            },
        )
        second_attempted = asyncio.Event()

        async def project_second() -> None:
            async with sessions() as second_session:
                second_attempted.set()
                await ConversationProjector(second_session).project(
                    thread_pk=thread_pk,
                    envelope=second_envelope,
                    event=second_event,
                )
                await second_session.commit()

        async with sessions() as first_session:
            await ConversationProjector(first_session).project(
                thread_pk=thread_pk,
                envelope=first_envelope,
                event=first_event,
            )
            second_task = asyncio.create_task(
                project_second(),
                name="test-concurrent-mysql-projector",
            )
            await second_attempted.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(second_task), timeout=0.2)
            await first_session.commit()
        await asyncio.wait_for(second_task, timeout=5)

        async with sessions() as verification_session:
            repository = ConversationRepository(verification_session)
            stored = await repository.get_thread_by_pk(thread_pk)
            assert stored is not None
            assert stored.last_seq == 2
            assert stored.snapshot_seq == 2
            assert stored.snapshot_json is not None
            assert stored.snapshot_json["snapshotSeq"] == stored.snapshot_seq
            assert stored.status == "idle"
            assert await repository.count_events(thread_pk) == 2
    finally:
        if second_task is not None and not second_task.done():
            second_task.cancel()
            await asyncio.gather(second_task, return_exceptions=True)
        if engine is not None:
            await engine.dispose()
        if created:
            await _drop_database(admin_engine, database_name)
        await admin_engine.dispose()
