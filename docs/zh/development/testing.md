# 仓库测试

[English](../../en/development/testing.md)

## 安装锁定的工作区

```bash
uv sync --locked --all-packages --group dev
```

开发依赖包含 Testcontainers 和 Docker SDK。它们只服务于仓库测试，不会进入发布包。

## 运行普通测试

```bash
uv run pytest
```

普通测试不会启动 Docker、连接本地开发服务或要求服务凭据。Docker 集成测试默认不运行。

## 运行 Docker 集成测试

```bash
uv run pytest packages apps/studio/server/tests -m docker_integration
```

该命令按需启动测试专属服务，并随机映射到宿主机回环端口：

- Redis Stack 的 DB 15 用于普通 Redis 契约，DB 0 用于 RediSearch checkpoint
- MySQL 8.4 提供管理数据库，Sandbox 与 Studio 测试分别创建随机命名的独立数据库
- OpenSandbox Server 使用随机 API key，并创建带测试标签的临时 Sandbox

测试不会读取外部 Redis 或 MySQL 连接变量。容器不使用持久卷，并带有本次运行专属的所有权
标签；正常执行和测试失败后，fixture teardown 会删除该标签下的完整资源集合。Docker daemon
不可用时，选中的 Docker 集成测试会给出明确原因并跳过。CI 会先检查 Docker，因此 daemon
不可用会直接导致集成任务失败。

OpenSandbox Server 需要挂载宿主机 Docker socket 来创建 runtime 容器。只应在可信仓库代码和
允许执行测试的 Docker daemon 上运行该集成测试。
