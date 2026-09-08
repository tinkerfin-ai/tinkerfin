# 运行第一个智能体

[快速开始](../quick_start.md) · [English](../../en/runtime/quick_start.md)

## 准备环境

需要 Python 3.11+ 和可调用示例模型的 OpenAI API 密钥。建议在独立虚拟环境中安装：

```bash
python -m venv .venv
source .venv/bin/activate
pip install tinkerfin langchain-openai
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境。

## 配置模型

将自己的密钥设置为 `OPENAI_API_KEY`。macOS / Linux：

```bash
export OPENAI_API_KEY="your-api-key"
```

Windows PowerShell 使用 `$env:OPENAI_API_KEY="your-api-key"`。
示例使用 `openai:gpt-5.4`；模型访问权限取决于你的服务账号，调用会按供应商规则计费。

## 运行

将下面的代码保存为 `hello.py`：

```python
import asyncio

from tinkerfin import RunIdentity, TinkerFin


async def main() -> None:
    runtime = TinkerFin()
    agent = runtime.create_deep_agent(model="openai:gpt-5.4", tools=[])
    stream = await runtime.open_run(
        RunIdentity(threadId="hello", runId="hello-1"),
        agent=agent,
        input={"messages": [{"role": "user", "content": "Say hello in one sentence."}]},
    )
    async for part in stream:
        print(part)


asyncio.run(main())
```

```bash
python hello.py
```

终端会逐步打印运行数据，其中包含模型的问候回复；具体文字由模型生成。
每次新运行使用新的 `runId`，同一会话可继续使用相同 `threadId`。
仅设置会话 ID 不会自动开启对话持久化；需要多轮记忆时配置 checkpointer，见 [Runtime 指南](index.md)。

## 下一步

- [添加模型与工具](deep-agents.md)
- [连接聊天前端](../agui/index.md)
- [记录执行过程](../tracing/index.md)
- [开发当前仓库](../development.md)
