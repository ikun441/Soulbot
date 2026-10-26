# 架构 · v0.9.2

入口 main.py → 构建配置/SQLite/密钥/Registry 服务 → NoneBot2 FastAPI + HTTPX + WebSocket 驱动 → OneBot V11 适配器 → 挂载管理 WebUI。ASGI 外层网关在 QQ 关闭时拒绝 OneBot 请求。

管理应用不处理消息，路由只调用共享服务。OneBot 适配器将消息转换为 core 标准事件；Agent 经访问规则、群聊决策、短期上下文和 LLM Provider，再由权限引擎在发送前复核动作。网页和 QQ 的模型调用经过相同的持久预算服务。

## 当前模块

- services/config_service：Pydantic Schema、原子 YAML、上一版备份与恢复。
- services/secret_service：`.env`/环境变量与 0600 JSON 密钥，API 仅提供配置存在标记。
- services/auth_service：加盐 PBKDF2、随机会话、会话摘要入库、失败登录限流。
- storage/database：SQLite 事务、schema_version、管理员、会话、向导、审计。
- providers/registry：能力维度注册/解析，跨能力不能替代。
- tools/registry：Pydantic 参数、默认拒绝权限、服务端调用上下文、超时与任务总次数限制、审计。
- web/static：原生 HTML/CSS/JS；后台状态与表单均使用真实 API。
- tools/emoji：目录内 ID 索引、检索和上传；media_tools 将结构化参数接到实际媒体服务。
- services/media_service：格式/尺寸/大小校验、24小时产物、QQ 会话范围隔离；WebUI 管理员使用登录鉴权读取。
- services/meme_service：固定版本协议客户端、后台准备、OS 单实例锁、自己创建的进程生命周期及有限恢复。
- scripts/meme_worker：在上游独立解释器运行，配置项目内数据路径和字体，不更改第三方源码。

## 状态与生命周期

运行根目录默认项目根，可用 SOULBOT_HOME 隔离。配置与数据库建立即幂等，不覆盖原文件。端口/QQ/Token 应用依赖重启；人格、预算、壁纸等管理配置保存后立即读取。每日额度记录持久化，外部操作在执行时重验权限。

OpenAI 兼容 LLM Provider 在请求时注册，模型 ID、工具能力和费用由服务管理员填写。网页试聊只请求文本；QQ 在能力和媒体权限允许时暴露已注册工具。任务总预算跨轮次共享，工具 Schema 计入输入。ToolContext 的会话范围由服务端注入，不接受 LLM 伪造；图片发送前重新检查权限。NoneBot 全局初始化只在真实入口执行，测试管理 API 使用独立应用。

## 语音识别路径
ASRService 与 TTSService 分开，能力 Registry 以 asr/tts 分类。ASR 先验证音频并预留秒数和费用，本地模型通过自有子进程加载，云端接口使用独立密钥。QQ record 经单独预过滤、去重和持久冷却，再调用 NapCat WAV/Base64；授权重验后转写作为普通用户内容进入 Agent，不能直接执行管理命令。上传测试不保存录音/转写历史，用量表仅有秒数和费用。

## 云端图片生成
ImageGenerationService 按每次一张预留持久额度与共享费用。独立 image_generation Provider 只返回经验证的 Base64 图片，HTML Render 不参与故障替代；服务返回 URL 不下载。generate_image 的服务端上下文决定产物范围，发送执行器和管理端通用图片发送都会按来源再次检查开关/权限。
