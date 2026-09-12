# 仓库开发

[English](../en/development.md)

在仓库根目录执行以下命令。需要 Python 3.11 或更高版本以及 uv。

提交 PR 和运行常规 Python、Studio Web 检查的步骤见[参与开发](../CONTRIBUTING.cn.md)。

## 安装工作区

```bash
uv sync --locked --all-packages --group dev
```

## 验证 Studio Web

在 `apps/studio/web` 执行 `pnpm test:browser`，构建应用并运行浏览器测试。
直接运行指定测试文件时，先构建应用：

```bash
pnpm build
pnpm exec playwright test tests/browser/todo-trace.spec.ts --workers=1
```

## 构建 wheel

统一构建命令生成十个框架包和 Studio 服务端的 wheel：

```bash
uv run --no-project --python 3.11 python scripts/build_wheels.py --out-dir dist
```

输出目录不能包含已有 wheel。只构建部分项目时，传入相对于仓库根目录的路径：

```bash
uv run --no-project --python 3.11 python scripts/build_wheels.py \
  packages/tinkerfin-contracts packages/tinkerfin-native-stream \
  --out-dir dist/selected
```

命令将当前项目文件复制到临时目录，包含未提交的改动和未跟踪的源码文件，排除构建目录、
缓存及自动生成的包元数据。构建不会修改或删除工作树中的这些文件。
准备发布产物时应暂停编辑源码，保证各项目使用一致的输入。

每个 wheel 必须与临时副本中的源码逐字节一致，核对范围包括 Python 模块、类型声明、
类型标记和包资源。所有选定项目都通过核对后，命令才向输出目录写入 wheel。
缺少文件、多出文件或内容不同都会导致命令失败。uv 缓存已包含构建依赖时，可添加 `--offline`。

本地构建、打包测试和 Studio Dockerfile 都调用此入口。
Dockerfile 在容器内导出锁定的生产依赖并构建 wheel。

## 验证打包

```bash
uv run --locked --no-sync pytest tests/packaging -m packaging_e2e
```

测试覆盖构建残留、当前源码内容、wheel 元数据、许可证、依赖声明和隔离环境安装。
CI 在 Python 3.11–3.14 上运行核心安装场景，另在 Python 3.11 上运行全部可选依赖组合和
Studio 部署所需的完整 wheel 集合。

## 验证 Docker 集成

启动 Docker 后执行以下测试；测试会创建并清理专用的临时服务：

```bash
uv run pytest -m docker_integration
```
