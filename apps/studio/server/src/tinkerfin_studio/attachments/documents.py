"""可取消、有超时和并发限制的文档处理入口"""

from __future__ import annotations

import asyncio
import json
import sys

import anyio
from pydantic import JsonValue, TypeAdapter

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class DocumentProcessor:
    """拥有每次文档子进程，超时或取消时终止并等待回收，最多并行两个"""

    def __init__(self) -> None:
        self._capacity = anyio.CapacityLimiter(2)

    async def run(self, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """处理一个有界文档任务，不把解析阻塞传播到 HTTP 或 Agent 事件循环"""
        async with self._capacity:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tinkerfin_studio.attachments.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                with anyio.fail_after(30):
                    stdout, _ = await process.communicate(
                        json.dumps(payload, ensure_ascii=False).encode()
                    )
                result = _JSON_OBJECT.validate_json(stdout)
                if process.returncode != 0:
                    raise ValueError(str(result.get("error", "文件处理失败")))
                return result
            finally:
                if process.returncode is None:
                    process.kill()
                with anyio.CancelScope(shield=True):
                    await process.wait()
