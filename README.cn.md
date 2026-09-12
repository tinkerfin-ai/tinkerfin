<p align="center">
  <img src="apps/studio/web/public/brand/tinkerfin-mark.png" alt="TinkerFin" width="88" />
</p>
<h1 align="center">TinkerFin</h1>
<p align="center"><strong>构建与交付企业智能体应用。</strong></p>
<p align="center">
  <a href="README.md">English</a> · <a href="README.cn.md">简体中文</a> ·
  <a href="docs/cn/index.md">Documentation</a> · <a href="docs/cn/quick_start.md">Quick Start</a>
</p>
<p align="center">
  <a href="docs/cn/runtime/quick_start.md"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" /></a>
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-52617A?style=flat-square" /></a>
  <a href="https://github.com/tinkerfin-ai/tinkerfin-harness/actions/workflows/packages-quality.yml"><img alt="Package checks" src="https://github.com/tinkerfin-ai/tinkerfin-harness/actions/workflows/packages-quality.yml/badge.svg" /></a>
  <a href="docs/cn/index.md"><img alt="Docs: English / 中文" src="https://img.shields.io/badge/Docs-English%20%2F%20中文-2563EB?style=flat-square" /></a>
</p>

## 项目说明

**TinkerFin 是面向企业业务的智能体应用开发框架。** 围绕任务编排、人机协同、执行追踪与隔离运行，帮助团队将业务流程构建为**可规划、可执行、可追踪、可干预**的智能体应用。

从调研分析、文档整理到数据处理与文件生成，把模型能力接入你的业务流程。
**以 Python 框架构建应用核心，以 Studio 交付业务工作台**，从模型与工具集成，到用户交互、过程管理和结果交付，减少从零搭建应用基础能力的工作。

![Studio 对话、任务清单与报告演示](docs/assets/screenshots/studio-cn.png)

## 安装

安装 Python 框架和要使用的模型集成：

```bash
pip install tinkerfin langchain-openai
```

Studio 使用 Docker 与 Docker Compose，[Studio 上手指南](docs/cn/studio/quick_start.md)
说明所需服务和配置。

## 快速开始

### 体验 Studio

通过 Docker Compose 启动后端，再单独启动 Web 客户端。初始账号为 `tinkerfin`，密码为 `123456`；登录后配置自己的模型。

[打开 Studio 上手指南 →](docs/cn/studio/quick_start.md)

### 接入 Python 框架

需要 Python 3.11 或更高版本。配置模型密钥后，通过 `TinkerFin().with_namespace(...).build(...)` 构建
`AgentRuntime` 并接收执行结果。

[运行第一个智能体 →](docs/cn/runtime/quick_start.md)

## 核心能力

- **按需组合，接入自己的应用** — 按需配置模型、工具、技能和子智能体，组合运行管理、消息传递、执行追踪与沙箱能力。可搭配自己的前端，也可使用 Studio。
- **先审计划，再执行任务** — Plan 模式先澄清需求、生成计划，经用户确认后交给智能体执行。关键工具操作可单独审批，多步骤任务中也能保留人工判断。
- **实时事件与断线续传** — 将智能体输出转换为 AG-UI 事件，通过消息通道持久化、推送与回放。业务代码可在运行期间发布自定义事件，与智能体输出共用订阅和回放机制。
- **从一次对话追到每次调用** — 关联模型、工具和子智能体的调用关系、输入输出与耗时。既能实时查看进展，也能查询历史记录，定位失败步骤和耗时环节。
- **为智能体管理独立工作环境** — 在隔离环境中读写文件、执行命令，支持环境复用、预热、暂停和恢复。由应用决定环境如何分配，框架负责连接与资源生命周期。
- **持久状态，支持多进程运行** — 按需接入检查点、存储与运行协调，保存会话状态并在审批后继续执行；统一处理多进程下的运行归属、重复请求与取消。
- **多租户集成** — 支持应用按租户、用户或项目划分会话与沙箱，使用存储命名空间区分数据范围。身份认证与访问授权由应用负责。
- **运行与调度后台任务** — 立即执行宿主注册的操作，或设置一次性、固定速率和 Cron 调度；通过 [Automation](docs/cn/automation/index.md) 查看结果、取消执行或发起重试。

## Studio：支持多模态的智能体工作台

用文字、图片和文档发起任务，在对话中完成分析、生图与文件交付。看图和生图需配置相应模型。

### 将业务目标转为执行计划

业务目标先转成可审阅的计划，确认范围和步骤后再进入执行。

![Studio 计划确认演示](docs/assets/screenshots/plan-cn.png)

### 掌握任务执行过程

从业务对话一路追踪到模型、工具和子智能体，查看任务如何执行、时间花在哪里。

![Studio 执行链路演示](docs/assets/screenshots/trace-cn.png)

*以上为当前 Studio 界面的演示数据截图。*

## 项目组成

| 部分 | 用途 |
| --- | --- |
| `packages/` | 企业智能体应用的核心框架，支撑任务编排、人机协同、执行追踪与隔离运行 |
| `apps/studio/` | 可部署、可扩展的智能体工作台，统一业务交互、计划审批与执行追踪 |
| `docs/` | 覆盖应用搭建、部署运维与深度集成的双语文档 |

## 文档

[中文文档首页 →](docs/cn/index.md)

从应用搭建到部署运维，提供上手指南、架构说明与集成参考。

## 参与贡献

欢迎提交问题、改进文档或贡献代码。开发环境和验证命令见[开发指南](docs/cn/development.md)。

- [参与开发](docs/CONTRIBUTING.cn.md)
- [安全漏洞报告](SECURITY.md)

## 许可证

仓库默认采用 [Apache License 2.0](LICENSE)。
`tinkerfin-langgraph-store` 采用包内 [MIT License](packages/tinkerfin-langgraph-store/LICENSE)
与 [NOTICE](packages/tinkerfin-langgraph-store/NOTICE)。
