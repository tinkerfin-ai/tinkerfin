import type { TranslationKey } from '../../i18n'

export const modelTestMessages: Record<string, TranslationKey> = {
  model_listed: '基础检查通过，服务已列出该模型',
  models_unavailable: '还不能确认这个模型能否使用',
  model_not_listed: '还不能确认这个模型能否使用',
  text_received: '已收到文字回复',
  vision_response_received: '已收到看图回复，请核对识别内容',
  image_received: '测试图片已生成',
  empty_response: '服务没有返回可展示的内容',
  invalid_response: '服务返回格式不符合当前接口要求',
  response_too_large: '测试结果超过大小限制',
  timeout: '测试超时，请检查服务后重试',
  authentication_failed: '服务拒绝了认证，请检查密钥及接口权限',
  rate_limited: '服务请求受限，请稍后重试',
  service_error: '模型服务返回错误，请检查配置和服务状态',
  network_error: '无法连接模型服务，请检查地址和网络',
  endpoint_not_allowed: '该地址不在管理员允许的访问范围内',
  invalid_configuration: '配置不完整或参数不合法，请检查后重试',
}

