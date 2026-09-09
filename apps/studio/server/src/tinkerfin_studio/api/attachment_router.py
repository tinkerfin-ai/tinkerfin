"""经过用户认证的附件上传、读取与草稿删除入口"""

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from tinkerfin_contracts.media import Attachment
from tinkerfin_studio.api.dependencies import UserContextDep
from tinkerfin_studio.api.responses import ApiResponse
from tinkerfin_studio.resources import get_resources

router = APIRouter(prefix="/attachments", tags=["附件"])


@router.post("", response_model=ApiResponse[Attachment])
async def upload_attachment(
    request: Request,
    user: UserContextDep,
    name: Annotated[str, Query(min_length=1, max_length=255)],
) -> ApiResponse[Attachment]:
    """接收原始文件流，文件名通过查询参数传入并由服务端校验"""
    service = get_resources(request.app).attachments
    await service.cleanup()
    return ApiResponse.success(
        await service.upload(user_id=user.user_id, name=name, chunks=request.stream())
    )


@router.get("/{attachment_id}/content")
async def read_attachment(
    attachment_id: str,
    request: Request,
    user: UserContextDep,
    variant: Literal["original", "preview"] = "original",
) -> Response:
    """每次读取核验权限，原件以下载方式交付，预览只返回生成的 JPEG"""
    descriptor, data = await get_resources(request.app).attachments.read(
        attachment_id, user_id=user.user_id, variant=variant
    )
    return Response(
        data,
        media_type="image/jpeg" if variant == "preview" else descriptor.mime_type,
        headers={
            "Content-Disposition": f"{'inline' if variant == 'preview' else 'attachment'}; filename*=UTF-8''{quote(descriptor.name, safe='')}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{attachment_id}", response_model=ApiResponse[None])
async def delete_attachment(
    attachment_id: str, request: Request, user: UserContextDep
) -> ApiResponse[None]:
    """删除尚未发送的本人附件"""
    await get_resources(request.app).attachments.remove_draft(
        attachment_id, user_id=user.user_id
    )
    return ApiResponse.success()
