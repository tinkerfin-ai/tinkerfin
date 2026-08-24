"""forwardedProps command 契约的数据迁移"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tinkerfin_studio.conversation.models import ConversationEvent, ConversationRun


@dataclass(frozen=True, slots=True)
class CommandMigrationResult:
    """command 契约迁移的统计结果"""

    runs: int
    events: int
    active_runs: int


def migrate_run_input(value: dict[str, object]) -> tuple[dict[str, object], bool]:
    """把主 run 输入中的旧 mode 转换为 command.plan"""

    migrated = deepcopy(value)
    return _migrate_forwarded_props(migrated)


def migrate_run_started_event(
    value: dict[str, object],
) -> tuple[dict[str, object], bool]:
    """把 RUN_STARTED 输入中的旧 mode 转换为 command.plan"""

    migrated = deepcopy(value)
    event_input = migrated.get("input")
    if not isinstance(event_input, dict):
        return migrated, False
    _, changed = _migrate_forwarded_props(event_input)
    return migrated, changed


def _migrate_forwarded_props(
    value: dict[str, object],
) -> tuple[dict[str, object], bool]:
    forwarded_props = value.get("forwardedProps")
    if not isinstance(forwarded_props, dict) or "mode" not in forwarded_props:
        return value, False
    if "command" in forwarded_props:
        raise ValueError("forwardedProps 同时包含 mode 与 command")
    mode = forwarded_props["mode"]
    if mode not in {"default", "plan"}:
        raise ValueError(f"无法迁移未知 forwardedProps.mode: {mode!r}")
    forwarded_props["command"] = {"plan": "on" if mode == "plan" else "off"}
    del forwarded_props["mode"]
    return value, True


async def migrate_forwarded_commands(
    session: AsyncSession,
    *,
    apply: bool,
) -> CommandMigrationResult:
    """检查或迁移当前数据库中的 forwardedProps command 契约

    Args:
        session: 独占本次迁移事务的数据库会话
        apply: 是否把已校验的转换写回数据库

    Returns:
        受影响的 run、事件与当前活跃 run 数量

    Raises:
        ValueError: 数据无法无损转换，或写入时仍存在活跃 run
    """

    active_runs = int(
        await session.scalar(
            select(func.count())
            .select_from(ConversationRun)
            .where(ConversationRun.status == "running")
        )
        or 0
    )
    if apply and active_runs:
        raise ValueError(f"仍有 {active_runs} 个活跃 run，禁止执行迁移")

    changed_runs = 0
    runs = await session.scalars(
        select(ConversationRun).where(ConversationRun.input_json.is_not(None))
    )
    for run in runs:
        if run.input_json is None:
            continue
        migrated, changed = migrate_run_input(run.input_json)
        if not changed:
            continue
        changed_runs += 1
        if apply:
            run.input_json = migrated

    changed_events = 0
    events = await session.scalars(
        select(ConversationEvent).where(ConversationEvent.event_type == "RUN_STARTED")
    )
    for event in events:
        try:
            event_text_json = json.loads(event.event_text)
        except json.JSONDecodeError as error:
            raise ValueError(f"事件 {event.id} 的 event_text 不是有效 JSON") from error
        if event_text_json != event.event_json:
            raise ValueError(f"事件 {event.id} 的 event_json 与 event_text 不一致")
        migrated, changed = migrate_run_started_event(event.event_json)
        if not changed:
            continue
        changed_events += 1
        if apply:
            event.event_json = migrated
            event.event_text = json.dumps(
                migrated,
                ensure_ascii=False,
                separators=(",", ":"),
            )

    return CommandMigrationResult(
        runs=changed_runs,
        events=changed_events,
        active_runs=active_runs,
    )
