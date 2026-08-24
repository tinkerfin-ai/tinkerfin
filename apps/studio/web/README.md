# TinkerFin Studio Web 客户端

该目录包含 React 19 与 TypeScript 客户端，已实现登录、会话列表、AG-UI SSE 对话、
历史恢复、任务与 Tool 状态、子 Agent 展示和 HITL 中断恢复。

## 本地运行

运行环境须满足以下任一 Node.js 版本范围：

- 20.x：20.19.0 或更高版本
- 22.x 及后续主版本：22.12.0 或更高版本

项目固定使用 pnpm 10.8.0。开发服务器默认把 `/api` 代理到
`http://127.0.0.1:8090`。

```bash
pnpm install --frozen-lockfile
pnpm dev
```

连接其他后端时可设置 API 基础地址：

```bash
VITE_API_BASE_URL=https://api.example.com pnpm dev
```

客户端会调用 `apps/studio/server` 提供的认证、数据库模型目录、会话、
历史记录、取消和 AG-UI 对话流接口。SSE 网络中断后，客户端使用服务端回填的
canonical threadId、原 runId 和最后一条持久化序号自动重连。

登录会话使用后端返回的 UTC `expires_at` 作为固定到期时间，不因页面操作或请求续期。
客户端会在定时到期、页面恢复和认证请求前检查该时间；到期或收到后端 401 时静默返回
登录页。页面启动校验遇到网络错误或服务端暂不可用时，会保持业务界面未挂载并自动重试，
不会清除仍可能有效的会话。一个标签页退出或到期后，其他同源标签页同步退出。

侧栏账户区使用认证响应中的 `avatar_url` 显示头像；地址为空或图片加载失败时显示稳定的
名称首字符占位。账户菜单中的“设置”展示头像、展示名和用户名，并提供跟随系统、浅色、
深色三种外观选项。会话页头部不再重复显示外观入口，登录页仍保留主题切换。

设置分为“账号管理”和“通用”。通用设置提供简体中文、English 和跟随系统三种语言偏好，
缺省使用简体中文，选择后立即作用于当前浏览器并同步同源标签页。跟随系统把所有中文变体
解析为简体中文，英语解析为 English，其他系统语言回退 English。语言切换只作用于前端
自有界面文案；后端响应、会话标题、消息、Plan、Todo、工具内容和代码始终保持原文。

新会话在侧栏以“新会话”显示，首个 chat 请求发送空 `threadId`。客户端生成本次请求的
`runId`，并给 user 消息填写 `request-${runId}`，以满足标准 AG-UI 消息校验。后端不把
这个客户端消息 ID 作为业务身份；服务端在 `RUN_STARTED` 中返回已入库的 canonical
`threadId`、标题和带权威消息 ID 的完整 input，客户端据此替换草稿状态。

## 输入与命令

消息输入卡片的底部工具行提供本地附件、Plan 状态、模型选择和发送或停止操作。模型选择
只展示后端目录中的可用模型，并随每次请求发送稳定模型 ID。

输入 `/plan` 会在本地开启 Plan，不产生消息请求；输入 `/plan <消息>` 会开启 Plan，并只把
去掉命令前缀后的正文发送给后端。Plan 开启后，工具行显示黄色 `Plan` 状态按钮；该按钮是
关闭 Plan 的唯一入口，`/plan off` 不作为关闭指令执行。

本地附件入口支持 PNG、JPEG、WebP、GIF 和 PDF，最多 5 个、单个不超过 10MB、合计不超过
25MB。附件只用于浏览器本地预览，不进入 chat、resume 或其他 AG-UI 请求；发送文字后仍保留
在当前会话的输入区，切换、新建或删除当前会话时清空。

每次 start 与 resume 请求都使用 `forwardedProps.command.plan` 声明有效 Plan 状态：`on`
对应内部 `plan` 模式，`off` 对应内部 `default` 模式。HTTP 请求不再发送
`forwardedProps.mode`。

默认代理目标是 `http://127.0.0.1:8090`。需要临时连接其他本地端口时设置：

```bash
VITE_API_PROXY_TARGET=http://127.0.0.1:8092 pnpm dev
```

## 质量检查

```bash
pnpm test
pnpm lint
pnpm build
```

## 安全限制

当前客户端把 bearer token 保存在浏览器 `localStorage`，页面脚本可以读取该令牌，
因此不能直接作为生产认证边界。生产部署需改用 `HttpOnly`、`Secure`、`SameSite`
Cookie，并配套实现 CSRF 与 XSS 防护。
