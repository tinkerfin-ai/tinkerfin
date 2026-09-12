"""前端可用模型目录路由"""

import asyncio
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Request

from tinkerfin_studio.api.dependencies import (
    ModelServiceDep,
    RawTokenDep,
    UserContextDep,
    get_auth_service,
    get_auth_session,
)
from tinkerfin_studio.api.errors import BusinessException, ModelErrorCode
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.auth.types import UserContext
from tinkerfin_studio.infrastructure._failures import _cleanup_failure_priority
from tinkerfin_studio.models.schemas import (
    AgentModelCatalog,
    AgentModelSave,
    AgentModelSettings,
    ModelTestRequest,
    ModelTestResult,
)
from tinkerfin_studio.models.testing import run_model_test
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/models", tags=["模型"])


@router.get("", response_model=ApiResponse[AgentModelCatalog], summary="模型目录")
async def list_models(
    user: UserContextDep,
    service: ModelServiceDep,
) -> ApiResponse[AgentModelCatalog]:
    """返回当前允许创建新 run 的安全模型目录"""

    del user
    return ApiResponse.success(await service.list_catalog())


@router.get("/configurations", response_model=ApiResponse[list[AgentModelSettings]])
async def model_settings(
    service: ModelServiceDep,
) -> ApiResponse[list[AgentModelSettings]]:
    """返回当前用户的可编辑模型配置，密钥不回显"""
    return ApiResponse.success(await service.settings())


@router.put("/configurations/{model_id}", response_model=ApiResponse[None])
async def save_model_settings(
    model_id: str, payload: AgentModelSave, service: ModelServiceDep
) -> ApiResponse[None]:
    """保存当前用户的模型或生图服务，不能访问其他用户的同名配置"""
    if payload.model_id != model_id:
        raise BusinessException(
            ModelErrorCode.INVALID_CONFIGURATION, message="模型标识与请求路径不一致"
        )
    await service.save_settings(payload)
    return ApiResponse.success()


@router.put("/configurations/{model_id}/default", response_model=ApiResponse[None])
async def set_default_model(
    model_id: str, service: ModelServiceDep
) -> ApiResponse[None]:
    """启用本人已保存模型并设为同用途默认项，保持连接配置不变"""
    await service.set_default(model_id)
    return ApiResponse.success()


@router.delete("/configurations/{model_id}", response_model=ApiResponse[None])
async def delete_model_settings(
    model_id: str, service: ModelServiceDep
) -> ApiResponse[None]:
    """删除本人模型配置，保留会话历史"""
    await service.delete_settings(model_id)
    return ApiResponse.success()


async def _test_user(request: Request, token: RawTokenDep) -> UserContext:
    """测试前完成认证并归还连接，外部模型等待不占用认证数据库会话"""
    async with get_resources(request.app).database.session() as session:
        service = await get_auth_service(request, session)
        return (await get_auth_session(token, service)).user


@router.post("/configurations/test", response_model=ApiResponse[ModelTestResult])
async def test_model_configuration(
    request: Request,
    payload: ModelTestRequest,
    user: Annotated[UserContext, Depends(_test_user)],
) -> ApiResponse[ModelTestResult]:
    """仅测试当前草稿；请求断开时取消本次任务，并等待客户端关闭"""
    resources = get_resources(request.app)

    async def watch_disconnect() -> None:
        while not await request.is_disconnected():
            await asyncio.sleep(0.1)

    test = asyncio.create_task(
        run_model_test(
            resources.database,
            user_id=user.user_id,
            payload=payload,
            allowed_origins=resources.settings.model_allowed_origins,
        )
    )
    disconnected = asyncio.create_task(watch_disconnect())
    primary: BaseException | None = None
    try:
        done, _ = await asyncio.wait(
            (test, disconnected), return_when=asyncio.FIRST_COMPLETED
        )
        if test not in done:
            raise asyncio.CancelledError()
        return ApiResponse.success(await test)
    except BaseException as error:
        primary = error
        raise
    finally:
        tasks = (test, disconnected)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        cancellation = primary if isinstance(primary, asyncio.CancelledError) else None
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    await asyncio.wait(tasks)
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
                    if any(not task.done() for task in tasks):
                        continue
                break
        failures: list[BaseException] = [] if primary is None else [primary]
        if cancellation is not None and cancellation is not primary:
            failures.append(cancellation)
        for task in tasks:
            try:
                task.result()
            except BaseException as error:  # noqa: BLE001 - 收齐后一次性交付原始失败
                if error is not primary and _cleanup_failure_priority(error):
                    failures.append(error)
        if failures:
            chosen = next(
                (error for error in failures if _cleanup_failure_priority(error) == 2),
                cancellation or primary or failures[0],
            )
            remaining = [error for error in failures if error is not chosen]
            if remaining:
                secondary = (
                    remaining[0]
                    if len(remaining) == 1
                    else BaseExceptionGroup("模型测试与客户端清理同时失败", remaining)
                )
                try:
                    raise secondary
                except BaseException:  # noqa: BLE001 - 保留取消及各异常原始cause
                    raise chosen
            raise chosen
