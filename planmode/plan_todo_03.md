# TinkerFin Plan Clarification 强类型业务 Schema 方案

## 1. 目标与边界

TinkerFin 提供通用 Plan Clarification 合同，并允许框架使用者在自己的宿主应用中定义具体 Pydantic Form：

```text
TinkerFin 通用模型
→ 宿主应用定义具体 Form
→ Gate / Planner 生成并校验具体 Form
→ JSON checkpoint
→ Plan Clarification interrupt
→ 用户回答
→ 使用同一具体 Form 重新校验并恢复
```

默认用户不需要定义任何业务模型：

```python
agent = (
    TinkerFin()
    .plan(enabled=True)
    .create_deep_agent(...)
)
```

只有需要业务 attributes 时，宿主应用才定义具体 Form：

```python
class AppClarificationForm(
    ClarificationForm[AppClarificationQuestion]
):
    pass


agent = (
    TinkerFin()
    .plan(
        enabled=True,
        clarification_schema=AppClarificationForm,
    )
    .create_deep_agent(...)
)
```

### 1.1 框架与业务边界

`../packages/tinkerfin` 只能包含：

- 通用 Clarification Pydantic 基类和泛型模型
- `DefaultClarificationForm`
- Schema 校验、结构化输出、checkpoint 和 resume 逻辑
- 通用 AG-UI 与 Studio 数据合同

部署地域、价格、风险、数据库类型等具体业务模型：

- 必须由框架使用者在宿主应用中定义
- 不得出现在 TinkerFin 默认配置或公共导出中
- 不得被 TinkerFin 解释为框架语义
- 仓库测试只能使用 `CustomClarificationForm` 等中性 Fixture 名称

### 1.2 非目标

- 不实现业务专属 Studio renderer
- 不把 attributes 当作计费、授权或合规的权威数据
- 不提供任意 `dict[str, JsonValue]` attributes 逃生口
- 不支持多选
- 不修改 Plan Review 的 approve/edit/respond/reject
- 不修改 Tool/Filesystem HITL
- 不实现 StageRun、Stage Guard、Final Guard 或执行中 Replan
- 不实现进程重启后的 Graph 自动重建
- 不新增数据库列
- 不兼容旧 Plan checkpoint 或 pending interrupt

## 2. 事实依据与当前实现

### 2.1 锁定版本

- Deep Agents 0.7.5
- LangGraph 1.2.10
- LangChain 1.3.14
- LangChain Core 1.5.3
- Pydantic 2.13.4
- AG-UI protocol 0.1.19

### 2.2 已验证依赖事实

- Pydantic 支持具体泛型 Form、嵌套 attributes、discriminated union 和 JSON round-trip
- 标准 `__parameters__` 可以识别未绑定泛型，不需要读取 Pydantic 私有 metadata
- LangChain `ToolStrategy(PydanticType)` 会返回经过校验的具体 Pydantic 实例
- `ToolStrategy(..., handle_errors=True)` 会让模型纠正结构化输出校验错误
- 裸 JSON Schema dict 只能得到普通 dict，不能得到具体 Pydantic 实例
- LangGraph interrupt payload 必须可 JSON 序列化
- interrupt 恢复时节点从开头重新执行，`interrupt()` 返回本次 `Command(resume=...)` 的输入
- interrupt 与 resume 必须使用同一个 checkpointer 和 `thread_id`
- `Command(resume=...)` 是下一次 Graph invocation 的输入，不是 stream event
- AG-UI interrupt 前必须保持 `STATE_SNAPSHOT → MESSAGES_SNAPSHOT → RUN_FINISHED(interrupt)`
- `status="cancelled"` 表示 abandon，不能转换为 reject
- `create_agent(checkpointer=None)` 的嵌套子图会继承父 saver，并在父节点后置
  round-trip 前持久化自身 `structured_response`；Gate/Planner 不产生 interrupt，因此使用
  `checkpointer=False`，由父节点只在具体 Form 完成 JSON round-trip 后写入 durable Plan state
- 锁定 LangGraph 1.2.10 会通过嵌套 RunnableConfig 传播父 `sync` durability；无 checkpointer
  Gate/Planner 调用必须在配置副本中移除锁定键 `__pregel_durability`，同时保留 parent
  runtime/store/context。该适配不得修改调用方 config，并必须由锁定版本与 store/context
  回归约束

官方依据：

- [LangChain structured output](https://docs.langchain.com/oss/python/langchain/structured-output)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Deep Agents human-in-the-loop](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop)
- [AG-UI interrupts](https://docs.ag-ui.com/concepts/interrupts)

### 2.3 当前仓库事实

当前实现为：

- `ClarificationModel`、Option/Question/Form Base、强类型泛型模型和
  `DefaultClarificationForm`
- `.plan(clarification_schema=...)` Definition 级冻结配置；run/resume 只能选择 mode
- Definition 创建期拒绝未绑定泛型、dict/Any attributes、可变 questions/options 容器和核心字段重定义
- Gate/Planner 使用绑定具体 Form 的 Pydantic 外层类型与
  `ToolStrategy(..., handle_errors=True)`，不再传裸 JSON Schema dict
- Gate/Planner 使用 `checkpointer=False`；DeepAgent 使用 `checkpointer=None` 继承父 saver
- PlanState v2、JSON-only pending/history、独立内部 Schema fingerprint channel 和同 Schema round-trip
- interrupt `responseSchema` 原生表达 Option ID-only 与 free-text only 两种精确对象
- 完整回答覆盖、重复/未知/过期 ID 拒绝，以及 checkpoint 可信 label 派生
- `tinkerfin.plan-clarification.v1` metadata、完整 Form/attributes、Studio 通用卡片和历史投影
- fingerprint 从所有公开 AG-UI state/task/checkpoint 事件剥离，但保留在 native checkpoint state
- Plan Review、Tool/Filesystem HITL、Plan abandon 和 same-thread mode 切换保持独立

## 3. 对原始 `todo.md` 的修正

### 3.1 具体业务 Form 不是框架类型

原文中的 `DeploymentClarificationForm` 只能视为宿主应用示例，不能成为 TinkerFin 实现、默认类型或公共导出。

最终命名规则：

- 框架默认：`DefaultClarificationForm`
- 文档中的宿主占位：`AppClarificationForm`
- 测试 Fixture：`CustomClarificationForm`

### 3.2 ToolStrategy 不能只接收裸 Form

Gate 仍然必须返回 `route` 和 `goal`，Planner 仍然必须区分 `clarify` 与 `draft`。

错误设计：

```python
ToolStrategy(AppClarificationForm)
```

最终设计：

```text
ToolStrategy(具体 GateDecision Pydantic 类型)
└── clarify 分支携带 AppClarificationForm

ToolStrategy(具体 PlannerOutcome Pydantic 类型)
└── clarify 分支携带 AppClarificationForm
```

`structured_response` 是具体 GateDecision 或 PlannerOutcome 实例，其 clarification 字段才是宿主配置的具体 Form 实例。

### 3.3 泛型上界必须提供核心字段

`QuestionT bound=BaseModel` 后不能安全访问 `id` 和 `options`。

框架增加通用、可实例化的 Pydantic Base：

- `ClarificationOptionBase`
- `ClarificationQuestionBase`
- `ClarificationFormBase`

泛型参数分别绑定这些 Base 或 `ClarificationModel`。不使用 ABC、无真实替换边界的 Protocol、宽泛 cast 或私有 Pydantic metadata。

### 3.4 自由文本字段统一命名

最终统一为：

```text
Python / Pydantic：allow_free_text
JSON / AG-UI / Studio：allowFreeText
```

不保留 `allow_custom_answer` 或 `allowCustomAnswer` alias、双读或 fallback。

精确语义：

| options | allow_free_text | 用户交互 |
| --- | --- | --- |
| 无 | `true` | 纯文本输入 |
| 无 | `false` | 非法 Form |
| 有 | `true` | 单选项或自由文本 |
| 有 | `false` | 必须选择一个预设项 |

`allow_free_text` 不控制：

- 模型是否生成 options
- 业务方使用哪个 Form
- 是否允许多选
- Plan Review 动作
- Tool HITL 决策

### 3.5 Option 回答只提交 ID

选择预设项：

```json
{
  "questionId": "target",
  "optionId": "option-a"
}
```

自由文本：

```json
{
  "questionId": "target",
  "answer": "用户自定义答案"
}
```

规则：

- `optionId` 与 `answer` 必须二选一
- 选择 Option 时由框架从 checkpoint 派生 label 和 attributes
- 客户端不得回传或覆盖 Form、Question、Option、label、description 或 attributes
- Option 路径的 `RequirementAnswer.answer` 使用 checkpoint 中的可信 label

### 3.6 Pydantic 校验不等于事实可信

attributes 只保证：

- 类型正确
- 枚举和范围正确
- Pydantic validator 通过
- 可以完成 JSON round-trip

它不能证明模型生成的价格、延迟、风险或地域信息与真实外部系统一致。

本阶段 attributes：

- 可以展示
- 可以交给 Gate/Planner 继续参考
- 不得直接驱动扣费、授权、资源创建或合规判断
- 不新增业务数据库查询或权威数据补全 hook

### 3.7 已有能力不重复实现

`RequirementAnswer.option_id`、动态选项、回答覆盖、PlanQuestionCard 等当前能力只在新合同需要时重构，不建立第二套平行模型或恢复路径。

### 3.8 混合问题必须保持框架 Form 边界

混合问题使用 Pydantic discriminated union，但最终 Form 仍必须继承 `ClarificationFormBase`，不能直接继承普通 `ClarificationModel` 绕过通用 Form 约束。

## 4. 公共 API 与通用模型

### 4.1 `.plan(...)`

新增参数：

```python
def plan(
    self,
    *,
    enabled: bool = True,
    default_mode: AgentMode = "default",
    gate_model: str | BaseChatModel | None = None,
    planner_model: str | BaseChatModel | None = None,
    clarification_schema: type[ClarificationFormBase]
        = DefaultClarificationForm,
) -> TinkerFin:
    ...
```

规则：

- 省略参数时行为与当前默认 Clarification 一致
- Schema 冻结在 `.plan(...)` 返回的不可变 factory 中
- `new/new_agui(mode=...)` 只能选择 mode，不能替换 Schema
- `astream` 和 resume 不能覆盖 Schema
- disabled Plan 不能配置自定义 Schema
- 普通非 Plan Definition 不受影响
- Deep Agents `create_deep_agent(...)` 参数保持原样

### 4.2 通用 Pydantic 模型

框架提供：

```text
ClarificationModel
├── frozen
├── extra="forbid"
├── camelCase alias
└── populate_by_name

ClarificationOptionBase
└── ClarificationOption[OptionAttributesT]

ClarificationQuestionBase
└── ClarificationQuestion[
        QuestionAttributesT,
        OptionAttributesT,
    ]

ClarificationFormBase
└── ClarificationForm[QuestionT]

DefaultClarificationForm
```

约束：

- attributes 类型必须继承 `ClarificationModel`
- Question 和 Option attributes 使用独立类型参数
- Form 至少包含一个 Question
- Question ID 在 Form 内唯一
- Option ID 在 Question 内唯一
- `allow_free_text=False` 时必须至少有一个 Option
- 核心回答规则不能隐藏在 attributes 中
- 默认 Form 使用无 attributes 的具体模型，不使用 dict

### 4.3 宿主应用职责

宿主应用可以：

- 定义具体 Question attributes
- 定义具体 Option attributes
- 定义具体 Option、Question 和 Form
- 使用 Pydantic field/enum/range/validator 表达本地业务约束
- 使用 discriminated union 组合不同 Question 类型

宿主应用不能：

- 修改 TinkerFin interrupt kind/origin
- 把 Plan status/revision/thread/run/checkpoint identity 放入 attributes
- 用 attributes 改写 Tool HITL allowed decisions
- 在单次 run 或 resume 时替换 Form Schema

### 4.4 Definition 创建期 Schema 校验

只使用标准 typing 和 Pydantic 公开接口：

- `issubclass(..., ClarificationFormBase)`
- `__parameters__`
- `model_fields`
- `typing.get_args/get_origin`
- `model_json_schema`

必须拒绝：

- 非 Pydantic Form
- 未绑定的 Form/Question/Option 泛型
- Question 不是 `ClarificationQuestionBase`
- Option 不是 `ClarificationOptionBase`
- attributes 不是具体 `ClarificationModel`
- attributes 使用 dict、Any 或未绑定类型
- 无法生成 JSON Schema

框架使用完整 `model_json_schema(by_alias=True)` 的 canonical JSON 自动计算 SHA-256 fingerprint。fingerprint 只用于 Definition 与 checkpoint 之间的内部结构兼容校验：

- 不要求业务方声明 `schema_id`
- 不进入公开 interrupt metadata
- 不要求 Studio 读取或展示
- 不作为安全签名或 attributes 真实性证明
- 只能识别 JSON Schema 形状变化；未体现在 JSON Schema 中的 Python validator 变化仍由 resume 时的当前具体 Pydantic Form 校验

## 5. Gate、Planner 与结构化输出

### 5.1 ClarificationSchemaBinding

增加内部冻结值对象 `ClarificationSchemaBinding`，保存：

- 具体 Form 类型
- Schema fingerprint
- 具体 Gate response Pydantic 类型
- 具体 Planner response Pydantic 类型

### 5.2 具体外层 Pydantic 类型

使用公开的 `pydantic.create_model`，基于通用 GateDecision/PlannerOutcome Base 创建绑定具体 Form 的外层类型。

这样可以：

- 保持 Gate/Planner 核心字段固定
- 让 LangChain 直接校验宿主应用 Form
- 避免把运行时业务类写死在框架源码
- 避免依赖 Pydantic 私有 generic metadata

Gate：

```python
ToolStrategy(
    binding.gate_response_type,
    handle_errors=True,
)
```

Planner：

```python
ToolStrategy(
    binding.planner_response_type,
    handle_errors=True,
)
```

规则：

- Gate direct/plan 分支不得携带 Form
- Gate clarify 分支必须携带配置的具体 Form
- Planner draft 分支不得携带 Form
- Planner clarify 分支必须携带配置的具体 Form
- `structured_response` 必须是绑定的具体外层类型
- clarification payload 必须是配置的具体 Form 类型
- 不传裸 JSON Schema dict
- 不提供文本、`json.loads` 或残缺 Form fallback
- Gate/Planner 使用 `checkpointer=False`，不得在父节点 JSON round-trip 前持久化 Pydantic
  `structured_response`
- DeepAgent 子图继续使用 `checkpointer=None` 继承父 saver，以保持 Tool/Filesystem HITL
- LangChain 最终失败传播原始异常
- 框架边界类型或 round-trip 异常使用 `PlanStructuredOutputError`

## 6. Checkpoint、interrupt 与 resume

### 6.1 PlanState v2

PlanState 升级为：

```text
workflow_version = "tinkerfin.plan.v2"
```

新增或重构为：

```text
内部 checkpoint channel
└── _tinkerfin_plan_clarification_schema: fingerprint

pending_clarification
├── source: gate | planner
└── form: JSON

clarification_history[]
├── source
├── form: JSON
└── normalized answers
```

内部 channel 不属于 `PlanState` 公共模型，并在 AG-UI STATE 事件发布前剥离。锁定版
LangGraph 的 native `values` 会包含所有普通 state channel，因此 Native Runtime 的低层对象
流仍可能携带这个框架保留 key；Studio、AG-UI state 和公开 Plan metadata 均不得包含它。

不同时保留可能漂移的第二套 questions/requirements 真相源。

### 6.2 写入 checkpoint

```text
具体 Pydantic Form
→ isinstance 确认
→ model_dump(mode="json", by_alias=True)
→ 同一具体 Form model_validate
→ 写 pending_clarification
→ checkpoint
→ interrupt
```

只有 round-trip 成功的数据才能成为 pending HITL。

### 6.3 resume

```text
读取 pending Form JSON
→ 从内部 checkpoint channel 校验 fingerprint
→ 使用 Definition 的同一 Form Schema model_validate
→ 解析 ClarificationResponse
→ 对照可信 Form 校验 questionId/optionId
→ 生成可信 RequirementAnswer
→ 移入 clarification_history
→ 清除 pending
→ 返回 Gate 或 Planner
```

必须验证：

- 回答完整覆盖 pending questions
- Question ID 不重复且存在
- Option ID 属于对应 Question
- `optionId` 与 `answer` 不得同时出现
- 自由文本只在 `allow_free_text=True` 时允许
- 旧 Question/Option/interrupt ID 被拒绝
- interrupt 仍属于当前 pending batch
- schema fingerprint 与 Definition 一致

Gate/Planner 从 clarification history 读取完整 Form、attributes 和标准化答案。

### 6.4 Plan clarification interrupt metadata

通用 `RuntimeInterruptEnvelope` 继续使用现有 v1。

Plan clarification metadata 使用框架级版本合同：

```json
{
  "origin": "plan",
  "source": "gate",
  "clarification": {
    "schema": "tinkerfin.plan-clarification.v1",
    "form": {
      "schemaVersion": 1,
      "questions": [
        {
          "id": "target",
          "prompt": "请选择目标",
          "options": [],
          "allowFreeText": true,
          "attributes": null
        }
      ]
    }
  }
}
```

规则：

- metadata 必须由 Pydantic 合同生成
- Schema fingerprint 只保存在 Plan checkpoint state，不得投影到公开 metadata
- Form 和 attributes 都是最终用户可见数据，不能包含密钥或内部权限信息
- adapter 只做无损转换、公开数据清理和恢复关联
- resume 的业务校验由父 Plan Graph 完成
- 保持 `STATE_SNAPSHOT → MESSAGES_SNAPSHOT → RUN_FINISHED(interrupt)`
- cancellation 继续表示 abandon，不制造 reject
- Tool/Filesystem HITL 合同不变

## 7. Studio 行为

Studio 本阶段只提供通用渲染：

- 展示 prompt、label、description
- 保存完整 Form JSON 和 Question/Option attributes
- 不解释或专门展示 attributes
- 不增加业务 renderer registry
- 未识别的业务字段不导致整个问题卡丢失
- Option 路径只提交 `questionId + optionId`
- 自由文本路径只提交 `questionId + answer`
- 不回传 Form、Question、Option、label、description 或 attributes
- Plan ReviewCard 和 Tool ApprovalCard 保持不变

TypeScript/JSON 命名必须使用 `allowFreeText`，不得继续读取 `allowCustomAnswer`。

## 8. 影响面

### `../packages/tinkerfin`

- 公共 Clarification 模型和 exports
- `.plan(...)` 与 PlanOptions
- Gate/Planner structured output
- PlanState 与 checkpoint JSON
- clarification interrupt/resume
- 新错误类型和错误码
- stub generator 与生成 stub

### `../packages/tinkerfin-agui-adapter`

- 从公开 STATE 事件剥离框架保留 fingerprint channel
- 通用 RuntimeInterrupt 实现保持不变
- 增加完整 Form/attributes、resume 和生命周期回归
- 保持 Plan/Tool interrupt 批次边界

### `../apps/studio/server`

- 不解析业务 attributes
- 保持完整 interrupt/history 投影
- 增加 Plan clarification v1 metadata 与恢复回归

### `../apps/studio/web`

- PlanQuestionState 保存完整 Form
- 通用 Question/Option 投影
- `allowFreeText`
- Option ID-only 和自由文本 resume payload
- history hydration 与 abandon 回归

### 文档与当前调用方

- `../packages/tinkerfin/README.md`
- Runtime/AG-UI 中英文文档
- API reference
- 当前示例、测试和类型检查 Fixture
- 所有旧 `allow_custom_answer` / `allowCustomAnswer` 当前调用点

### 全仓验证基础设施

- `../packages/tinkerfin-sandbox/tests/test_state.py` 只扩大既有 SQLite lease 续约测试的时间预算
- 生产 lease TTL、过期判断、重试和持久化语义不变

## 9. 实施步骤

- [x] 1. 创建并维护 `../.agents/plan/20260821192551-plan-clarification-schema.md`，记录目标、事实、步骤、回滚、验证与未核验项 ✅ 计划已随 fingerprint、checkpointer 和全仓短 TTL 验证事实同步修订
- [x] 2. 实现通用 Clarification Base、泛型 Question/Option/Form 和默认具体 Form ✅ 具体/混合 Form、唯一 ID、回答路径和非法 Schema 测试通过
- [x] 3. 将 `allow_custom_answer` 全链路替换为 `allow_free_text`，不保留 alias 或 fallback ✅ 生产代码、测试和文档陈旧命名搜索为零
- [x] 4. 扩展不可变 PlanOptions 与 `.plan(clarification_schema=...)`，完成公开 Schema 校验和内部 fingerprint ✅ disabled/custom、未绑定泛型、核心字段重定义、Schema 漂移测试通过
- [x] 5. 重构 GateDecision/PlannerOutcome 为可绑定具体 Form 的 Pydantic 外层合同，切换到具体 `ToolStrategy` ✅ 非法 attributes 触发 LangChain structured-output 纠正，Gate/Planner 无子图 checkpoint namespace
- [x] 6. 重构 PlanState v2、pending clarification、clarification history 和 JSON round-trip ✅ strict JsonPlusSerializer 与非法 serializer 失败关闭测试通过
- [x] 7. 修改 Gate/Planner clarification 创建与 resume，落实 Option ID-only、自由文本和可信派生 ✅ 精确 JSON Schema、显式 null、混合、缺失、重复和过期 ID 回归通过
- [x] 8. 增加版本化 Plan clarification metadata，保持通用 adapter、Plan Review 和 Tool HITL 独立 ✅ 真实 LangGraph v2→AG-UI 事件顺序、resume、abandon、异常、取消与唯一 terminal 回归通过
- [x] 9. 更新 Studio 通用解析、状态保存和 resume payload，不实现业务 renderer ✅ server 95 tests、Web 266 tests、ESLint 和 Vite build 通过
- [x] 10. 更新公共 exports、错误码、stub generator、生成 stub、包 README 和中英文文档 ✅ stub `--check`、wheel 内容、标准/strict Pyright 和中英文事实对齐通过
- [x] 11. 删除或改写所有把 Deployment 等具体业务类型描述为框架类型的示例；宿主示例统一使用 `App*`，测试统一使用 `Custom*` ✅ framework/test/docs 业务类型搜索为零
- [x] 12. 完成定向测试、真实 v2 流合同和相关全量验证 ✅ Plan/adapter 定向 105 passed；真实 Redis/MySQL8 全仓 1325 passed、0 skipped、19 subtests passed；Studio Web 266 passed
- [x] 13. 每一步仅在产物完成且对应验证通过后标记为 `- [x] ... ✅`，并附关键证据 ✅ 本节与内部执行计划在最终验证后同步完成

## 10. 验证计划

### 10.1 公共 API 与类型

- 默认 Form 开箱即用
- 宿主应用自定义具体 Form
- immutable `.plan(...)`
- disabled/custom Schema 组合
- 未绑定泛型和非法嵌套类型提前失败
- Pyright/PyCharm 能推断业务 attributes
- bound `create_deep_agent` 签名保持等于锁定 Deep Agents 0.7.5
- stub generator `--check`

### 10.2 Pydantic

- Question/Option ID 唯一
- attributes 未知字段、枚举、范围和 validator
- `allow_free_text=False` 时必须有 Option
- discriminated union
- 拒绝 dict/Any attributes
- JSON serialization 与同 Schema round-trip
- 非法 serializer 失败关闭

### 10.3 结构化输出

- Gate/Planner ToolStrategy 使用具体 Pydantic 外层类型
- 非法模型输出触发 LangChain 纠正
- 最终失败无文本 fallback
- direct/plan/draft/clarify payload 互斥
- Form 类型不匹配失败关闭
- 不出现业务具体类型对框架模块的反向依赖

### 10.4 Checkpoint 与 resume

- checkpoint 中只有 JSON 数据
- attributes、URL、tuple 等完成 round-trip
- schema fingerprint 不匹配失败
- Option 只提交 ID并派生可信 label
- 自由文本只提交 answer
- 同时提交 optionId/answer 被拒绝
- 缺失、重复、未知、过期 Question/Option ID 被拒绝
- Gate 和 Planner 回到各自正确节点
- PlanState v2 strict msgpack round-trip

### 10.5 Native v2 与 AG-UI

- `version="v2"`
- `stream_mode=["messages", "tasks", "values"]`
- `subgraphs=True`
- root/subgraph state 不混用
- 完整 Form/attributes 无损进入 interrupt
- state/messages snapshot 位于 interrupt terminal 前
- resume 使用下一次 `Command(resume=...)`
- abandon、异常、取消和唯一 terminal
- Tool/Filesystem HITL 全量回归
- `ag-ui-protocol==0.1.19` 事件模型校验

与本变更无关但必须明确标记为不适用的流项：

- 文本与 reasoning 内容映射逻辑不变
- tool call 参数增量拼接逻辑不变
- SubAgent/Todo 映射逻辑不变
- 这些项目运行现有回归，不声称由本变更新增验证

### 10.6 Studio

- 默认问题卡
- 有 options/无 options
- `allowFreeText=true/false`
- attributes 无损保存但不渲染
- Option ID-only payload
- 自由文本 payload
- 历史 hydration
- Plan abandon
- Plan ReviewCard 与 Tool ApprovalCard 不受影响

### 10.7 验证顺序

1. Plan、adapter、Studio server/web 定向测试
2. `uv run ruff format --check ...`
3. `uv run ruff check ...`
4. 全仓 Pyright
5. stub generator `--check`
6. Studio ESLint
7. Studio Vitest
8. Studio TypeScript/Vite build
9. 配置真实 Redis/MySQL 后运行全仓 pytest，目标 0 skipped
10. `git diff --check`

确定性完成标准以锁定依赖源码、Pydantic/ToolStrategy contract test 和真实 LangGraph v2 对象流为准，不依赖供应商 API 凭据。

## 11. 不兼容影响、回滚和完成标准

### 11.1 不兼容影响

- PlanState 从 v1 升级到 v2
- Plan clarification interrupt metadata 形状变化
- `allow_custom_answer` / `allowCustomAnswer` 被移除
- 新合同使用 `allow_free_text` / `allowFreeText`
- Option resume 不再接受同时提交的显示 answer
- 当前旧 checkpoint 不提供兼容读取
- 部署前必须清理或隔离旧 Plan thread/pending interrupt
- 普通非 Plan Definition、default mode、Plan Review 和 Tool HITL 不变

### 11.2 回滚

- 将 Clarification Schema、PlanState v2、Studio payload 和文档作为一个整体回退
- 不复用 v2 测试或本地 checkpoint
- 不通过 alias、双读、隐藏 fallback 或同时维护 v1/v2 恢复
- 回滚后重新验证当前默认 Plan Clarification 和普通 Deep Agent 路径

### 11.3 已验证边界

- attributes 只保证具体 Pydantic Schema、validator 和 JSON round-trip 通过，始终作为非权威参考，不直接驱动权限、计费或合规决策
- 内部 Schema fingerprint 采用完整 JSON Schema，类名、描述或字段变化都可能使 pending checkpoint 失效；这是保守失败关闭策略，不影响公开 interrupt 合同
- fingerprint 不能识别未进入 JSON Schema 的 validator 代码变化；resume 始终再执行当前具体 Pydantic Form 校验
- `uv run pyright` 与 `uv run pyright -p pyright-packages-strict.json` 均为 0 errors、0 warnings；strict 修复没有降低配置或排除文件
- 默认与自定义 Clarification Form 的 canonical Schema fingerprint 在 strict 类型修复前后完全一致
- 真实 Redis/MySQL8 全仓验证为 1325 passed、0 skipped、19 subtests passed
- 当前无数据库迁移
- 当前无阻塞项

### 11.4 完成标准

- TinkerFin 框架不包含任何具体业务 Clarification 类型
- 默认用户无需理解泛型或提供 Schema
- 高级用户可在宿主应用中提供一个具体 Pydantic Form
- `allow_free_text` / `allowFreeText` 全链路唯一且语义一致
- 任何未通过具体 Schema 与 JSON round-trip 的模型输出都不会进入 checkpoint
- 客户端不能篡改 Option label 或 attributes
- attributes 完整到达 Studio，但只作为非权威参考数据
- 同一 Definition 使用冻结的具体 Schema；checkpoint 通过独立内部 channel 的 fingerprint 检查结构漂移并用当前具体 Form 完成最终校验
- Plan Review 与 Tool/Filesystem HITL 保持独立
- 所有定向、类型、流、Studio 和相关全量验证通过
