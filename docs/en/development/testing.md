# Repository testing

[中文](../../zh/development/testing.md)

## Install the locked workspace

```bash
uv sync --locked --all-packages --group dev
```

The development group includes Testcontainers and the Docker SDK. These dependencies
are repository tooling and are not included in published packages.

## Run ordinary tests

```bash
uv run pytest
```

Ordinary tests do not open Docker, connect to local development services, or require
service credentials. Docker integration tests are excluded by default.

## Run Docker integrations

```bash
uv run pytest packages apps/studio/server/tests -m docker_integration
```

This command lazily starts test-owned services on random loopback ports:

- Redis Stack uses DB 15 for ordinary Redis contracts and DB 0 for RediSearch
  checkpoints.
- MySQL 8.4 provides a management database; Sandbox and Studio tests create separate
  randomly named databases.
- OpenSandbox Server uses a random API key and creates a labelled disposable Sandbox.

The tests do not read external Redis or MySQL connection variables. Containers have no
persistent volumes and carry one run-specific ownership label; fixture teardown removes
that complete ownership set after normal execution and test failures. If the Docker
daemon is not available, selected Docker integration tests are skipped with an explicit
reason. CI checks Docker first, so an unavailable daemon fails the integration job.

OpenSandbox Server mounts the host Docker socket to create its runtime container. Run
this integration only from trusted repository code on a test-capable Docker daemon.
