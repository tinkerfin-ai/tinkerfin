"""附件字节的异步持久化边界"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from anyio import to_thread


class DiskAttachmentStorage:
    """将原件与派生图保存在独立数据卷，不依赖 Sandbox

    文件 I/O 使用 AnyIO 的受限线程容量，替换操作也在同一边界执行。
    取消时等待当前短 I/O 完成，清理临时文件后继续传播取消。
    对象标识由服务端分配，禁止使用客户端文件名或路径。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}(?:-preview|-model)?", key):
            raise ValueError("附件存储标识不合法")
        return self._root / key

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> None:
        """接收服务层限量后的字节流，完整落盘后原子发布，取消时删除临时文件"""
        target = self._path(key)
        temporary = target.with_suffix(".partial")
        await anyio.Path(self._root).mkdir(parents=True, exist_ok=True)
        try:
            async with await anyio.open_file(temporary, "xb") as stream:
                async for chunk in chunks:
                    await stream.write(chunk)
                await stream.flush()
                await to_thread.run_sync(os.fsync, stream.wrapped.fileno())
            await to_thread.run_sync(os.replace, temporary, target)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.Path(temporary).unlink(missing_ok=True)

    async def read(self, key: str) -> bytes:
        """按服务端对象标识读取至多 10 MiB 加一个边界检测字节"""
        async with await anyio.open_file(self._path(key), "rb") as stream:
            return await stream.read(10 * 1024 * 1024 + 1)

    async def delete(self, key: str) -> None:
        """幂等删除对象及其未完成的临时文件"""
        target = self._path(key)
        await anyio.Path(target).unlink(missing_ok=True)
        await anyio.Path(target.with_suffix(".partial")).unlink(missing_ok=True)
