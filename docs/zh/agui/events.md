# 看懂 AG-UI 事件

[AG-UI 入门](index.md) · [English](../../en/agui/events.md)

一次正常运行从 `RUN_STARTED` 开始，以一个 `RUN_FINISHED` 或 `RUN_ERROR` 结束。中间事件按实际运行顺序到达。

## 最常见的事件

| 事件 | 前端通常怎么用 |
| --- | --- |
| `RUN_STARTED` | 建立运行状态，读取完整请求输入 |
| `TEXT_MESSAGE_START` | 创建一条新的 Agent 消息 |
| `TEXT_MESSAGE_CONTENT` | 追加回答文字 |
| `TEXT_MESSAGE_END` | 结束这条消息 |
| `TOOL_CALL_START` | 显示正在调用哪个工具 |
| `TOOL_CALL_ARGS` | 逐步追加工具参数 |
| `TOOL_CALL_END` | 工具参数已经完整 |
| `TOOL_CALL_RESULT` | 显示工具执行结果 |
| `STATE_SNAPSHOT` / `STATE_DELTA` | 更新前端状态 |
| `MESSAGES_SNAPSHOT` | 用完整消息快照校准历史 |
| `RUN_FINISHED` | 标记成功或等待用户处理 interrupt |
| `RUN_ERROR` | 标记运行失败 |

同一条消息或工具调用可能分成很多 delta。前端要按事件中的完整 ID 归并，不能按“谁先返回”猜测对应关系。

## 一个简单顺序

没有工具调用时，通常能看到：

```text
RUN_STARTED
TEXT_MESSAGE_START
TEXT_MESSAGE_CONTENT ...
TEXT_MESSAGE_END
STATE_SNAPSHOT / STATE_DELTA
RUN_FINISHED
```

有工具调用时，文字和工具事件可能交错；并行工具也可能同时处于开放状态。只要按 ID 更新各自的 UI 即可。

## 主 Agent 和子 Agent

子 Agent 来自非根 namespace。转换器会保留完整 namespace 和来源信息，避免不同子 Agent 的消息、工具和状态混在一起。

`expose_subagent_events=True` 会发送子 Agent 的公开事件。设置为 `False` 时，转换器仍会检查这些数据，但不把对应事件交给前端。

不要用 `parentRunId` 表示 LangGraph 子图。`parentRunId` 是调用方定义的运行关系；子图来源由事件自己的 namespace 和关联信息表示。

## 推理事件

如果界面需要显示支持的推理过程：

```python
runtime = agent.new_agui(
    run_input=run_input,
    expose_reasoning_events=True,
)
```

你可能收到 `REASONING_START`、`REASONING_MESSAGE_*` 和 `REASONING_END`。不是所有模型都会产生可公开的推理事件，也不能假设内容为空的模型 chunk 就是心跳。

无论开关是否启用，provider 私有推理元数据都不会作为普通状态、消息或 raw payload 直接公开。

## `messages`、`tasks`、`values` 分别做什么

这是服务端集成时最容易混淆的地方。

| 原生模式 | 转换时提供的信息 |
| --- | --- |
| `messages` | 文本 chunk、工具参数 chunk、工具结果及消息 metadata |
| `tasks` | Graph 节点和任务的开始、结果、错误及 namespace 关系 |
| `values` | 每一步之后的状态快照，以及顶层 `interrupts` |

`tasks` 是复数；它不等于 Deep Agents 中名为 `task` 的子 Agent 工具。根 Graph 与子图的 `values` 也是不同状态范围，前端或服务端不能用后到的子图状态覆盖根状态。

## 终止事件

每次主运行只应有一个终止事件：

- 成功完成：`RUN_FINISHED`，outcome 为 success；
- 等待审批：`RUN_FINISHED`，outcome 带 interrupts；
- 运行失败：`RUN_ERROR`。

文本、推理和工具生命周期会在主终止事件之前关闭。客户端断开时，应取消或关闭服务端流，不要自行伪造成功事件。

## 观察或转发事件

```python
async def audit_event(event) -> None:
    await audit_log.write(event.model_dump(mode="json", by_alias=True))


runtime = agent.new_agui(
    run_input=run_input,
    on_event=audit_event,
)
```

`on_event` 在事件交给消费者前执行。它适合审计和指标，不适合阻塞 I/O。

下一篇：[interrupt 与恢复](interrupts-and-resume.md)。

