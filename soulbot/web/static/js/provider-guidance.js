const guidance = {
 authentication: ['认证被拒绝', '检查该服务专用 API Key 是否有效，以及账号是否有模型权限。'],
 configuration: ['配置不完整', '填写独立密钥、模型 ID 和必需的音色，再保存配置。'],
 not_configured: ['服务尚未配置', '检查 Base URL、模型 ID、密钥和启用状态。'],
 model_not_found: ['找不到模型或接口', '核对模型 ID 与 Base URL，并确认账号可以访问该模型。'],
 parameters: ['服务不接受当前参数', '按该模型的服务商文档检查音色、语言、尺寸和返回格式。'],
 bad_request: ['服务拒绝了请求', '检查模型参数和上下文上限，并按服务文档确认兼容接口。'],
 unsupported_capability: ['当前服务不支持此能力', '切换到声明支持该能力的模型或 Provider。'],
 rate_limit: ['服务限流或额度不足', '查看服务商额度与限流状态，稍后再试。'],
 budget: ['旧版预算状态', '当前版本预算只预警；请刷新并查看用量记录。'],
 task_budget: ['旧版任务预算状态', '当前版本任务 Token 仅预警；请刷新并查看用量记录。'],
 unknown_cost: ['费用无法确认', '请核对服务商账单；当前仅预警，主动任务和普通调用均继续。'],
 quota: ['达到本地频率或每日额度', '查看用量页面，等待额度窗口恢复后再试。'],
 timeout: ['请求超时', '先核对服务状态和用量记录；服务端可能已处理请求，避免连续重试。'],
 cancelled: ['请求已取消', '取消只停止本地等待；检查用量记录和服务商账单后再决定是否重试。'],
 network: ['网络连接失败', '检查 Base URL、DNS、代理和网络连接；先查看用量记录再重试。'],
 unavailable: ['服务暂不可用', '稍后查看服务商状态；请求可能已经到达服务端，请先核对用量。'],
 internal: ['请求未能完成', '查看 SoulBot 操作记录和用量页面；若费用不确定，先核对服务商账单。'],
 invalid_response: ['服务返回内容不兼容', '检查模型和接口类型；确认服务返回符合所选能力的格式。'],
 format: ['返回格式无效', '确认模型支持所选音频或图片格式，再检查服务端响应设置。'],
 size: ['响应超过本地限制', '降低输出尺寸或时长后重试；服务端可能已生成并计费。'],
 url_only: ['服务只返回远程图片地址', '切换到支持 Base64 图片返回的模型；本地没有下载该地址。'],
 voice: ['找不到所选音色', '重新读取服务返回的音色列表，并从列表选择一个音色 ID。'],
 input: ['输入不符合限制', '缩短文本或提示词后再试。'],
 input_limit: ['输入或上下文过长', '缩短当前消息或人格 Prompt，或减少上下文。'],
 duration: ['录音超过时长限制', '截短录音并确认音频格式后再上传。'],
 local: ['本地模型处理失败', '确认 Whisper 模型已准备完成、依赖正常，并尝试缩短录音。'],
 not_ready: ['本地模型尚未准备', '在语音识别页面下载并检查本地 Whisper 模型。'],
 path: ['本地数据路径不安全', '检查 data/asr 目录及权限，移除符号链接后重试。'],
 busy: ['已有任务正在运行', '等待当前任务结束后再试。'],
 disabled: ['功能已关闭', '在对应 Provider 设置页启用功能并保存。'],
 invalid: ['输入或返回内容无效', '检查文件格式、大小和服务返回格式。'],
};

const mayHaveCharged = new Set([
 'timeout', 'cancelled', 'network', 'unavailable', 'internal', 'invalid_response', 'format', 'size', 'url_only', 'invalid',
]);

export function providerErrorText(
 kind,
 fallback = '检查 Provider 配置、用量记录和服务状态。',
 uncertain = false,
) {
 const item = guidance[kind];
 if (!item) return `${fallback}${uncertain ? ' 请求可能已计费，请先核对用量记录。' : ''}`;
 return `${item[0]}。${item[1]}${uncertain || mayHaveCharged.has(kind) ? ' 这次请求可能已经计费，请先核对用量记录和服务商账单。' : ''}`;
}

export function providerRequestErrorText(error, fallback) {
 const kind = error?.kind || (error?.name === 'AbortError' ? 'cancelled' : error?.name === 'TypeError' ? 'network' : undefined);
 return providerErrorText(kind, fallback || error?.message, error?.uncertain);
}

export function providerStatusText(test, name) {
 const states = {
  disabled: '功能已关闭',
  unconfigured: '尚未配置完整',
  not_tested: '配置已保存，尚未实际请求测试',
  available: `最近实际请求成功 · ${test.last_test_at}`,
  failed: `最近实际请求失败。${providerErrorText(test.last_error_kind)}`,
 };
 return `${name}状态：${states[test?.state] || '暂时未知'} 配置存在不代表服务可用；测试结果只对应当前配置。`;
}
