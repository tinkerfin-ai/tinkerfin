# 术语表

[Documentation](index.md) · [English](../en/glossary.md)


| 名称 | 简单理解 |
| --- | --- |
| Agent | 调用模型和工具完成任务的程序 |
| Graph | Agent 实际执行的工作流 |
| Runtime | 一次 Graph 运行的控制对象 |
| Plan Mode | 独立完成需求澄清与计划审批，并在批准后交给原生 Deep Agent 执行的工作流 |
| stream | 运行过程中连续产生的数据 |
| RunIdentity | 只包含稳定 `threadId` 与一次运行 `runId` 的框架身份 |
| thread | 一段可继续的会话，对应 `RunIdentity.threadId` |
| run | thread 中的一次语义执行，对应 `RunIdentity.runId` |
| Trace | 由 Runtime 生命周期和校验后的 Native 事实生成的用户可读语义历史 |
| AG-UI | 前端和 Agent 交换运行事件的协议 |
| SSE | 服务端持续向浏览器发送事件的一种 HTTP 格式 |
| Messaging | 保存并投递事件流的组件 |
| Sandbox | Agent 可执行命令、操作文件的隔离环境 |
