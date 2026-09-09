import { apiClient, type ApiAxiosRequestConfig } from '../../../api/shared/http'
import type { Attachment } from './content'

export async function uploadAttachment(
  file: File,
  signal: AbortSignal,
  onProgress: (percent: number) => void,
): Promise<Attachment> {
  const config: ApiAxiosRequestConfig = {
    url: '/api/attachments',
    method: 'POST',
    // 使用浏览器原生上传进度，在 HTTP/1.1 下也能发送文件并响应取消
    adapter: 'xhr',
    params: { name: file.name },
    data: file,
    headers: { 'Content-Type': 'application/octet-stream' },
    signal,
    suppressGlobalError: true,
    onUploadProgress: ({ loaded, total }) =>
      onProgress(
        Math.min(99, Math.round((100 * loaded) / (total || file.size || 1))),
      ),
  }
  return (await apiClient.request<Attachment>(config)).data
}

export async function attachmentBlob(
  id: string,
  variant: 'original' | 'preview',
  signal?: AbortSignal,
): Promise<Blob> {
  const config: ApiAxiosRequestConfig = {
    url: `/api/attachments/${encodeURIComponent(id)}/content`,
    params: { variant },
    responseType: 'blob',
    // 图片读取超时后显示重试入口，避免预览和下载一直等待
    timeout: variant === 'preview' ? 30_000 : 60_000,
    signal,
    suppressGlobalError: true,
  }
  return (await apiClient.request<Blob>(config)).data
}

export async function removeDraftAttachment(id: string): Promise<void> {
  const config: ApiAxiosRequestConfig = {
    url: `/api/attachments/${encodeURIComponent(id)}`,
    method: 'DELETE',
    suppressGlobalError: true,
  }
  await apiClient.request<null>(config)
}
