# 第三方接入核查 · 2026-10-24

## NoneBot2 / OneBot / NapCat

已阅读 [NoneBot 快速上手](https://nonebot.dev/docs/quick-start)、[驱动配置](https://nonebot.dev/docs/advanced/driver)、[OneBot 接入](https://onebot.adapters.nonebot.dev/docs/guide/setup/)、[NapCat 官方安装入口](https://napneko.github.io/guide/napcat)。

反向 WS 由 NapCat 连接 SoulBot，路径 /onebot/v11/ws；正向由 SoulBot 连接 NapCat 服务端。协议探测必须收到对应 echo 的 get_status 回执，不以握手代替成功。M1 的适配器可以发送 OneBot V11 文本消息并要求平台回执；真实 QQ 端到端联调仍需运行 NapCat 和已登录 QQ 账户。

## meme-generator（M2 强制交付）

已阅读官方 [README](https://github.com/MemeCrafters/meme-generator)、[本地安装](https://github.com/MemeCrafters/meme-generator/wiki/本地安装)、[API Wiki](https://github.com/MemeCrafters/meme-generator/wiki/通过-api-方式调用)、[命令行 Wiki](https://github.com/MemeCrafters/meme-generator/wiki/通过命令行使用)、[pyproject](https://github.com/MemeCrafters/meme-generator/blob/main/pyproject.toml)。

目前上游 Python 包元数据为 0.1.14、Python ^3.9，MIT 许可证。包含 Pillow/skia 等依赖与图片/字体资源。API 示例使用 multipart 图片、texts 和 JSON 编码 args。官方启动入口 meme run（或 python -m meme_generator.app），默认文档端口 2233，另有资源下载命令。

M2 将独立环境安装固定版本并真实验证 /openapi.json、模板 keys/info/preview 和生成接口，不把需求中的初步 URL 当作已验证契约。托管需要单实例锁、健康检查、退避重启、优雅终止和外部模式；资源与第三方错误不得影响核心。

**当前尚未安装/集成/运行生成验收**，M2 才提供 API；本阶段不会返回伪造模板或图片结果。

## 其它 Provider

LLM 以 HTTPX 实现 OpenAI 兼容 Chat Completions，可查询服务商模型、按需 SSE、解析服务商用量，按用户填写的价格估算费用。限流和服务端错误会按有限策略重试；未知或可能已计费结果保留预算。当前网页和 OneBot 消息只请求文本生成，不向模型开放可执行 Tool Calling。实际服务需要用户凭据。TTS/ASR/Image/Search 于 M2 实现；HTML Render 是模板渲染能力，不能作为自由生图 Provider 的自动等价替代。QQ 空间接口待 M5 验证，不支持时只保留草稿。
