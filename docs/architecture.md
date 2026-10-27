# 架构 · v2.0.0

入口 main.py → 构建配置/SQLite/密钥/Registry 服务 → NoneBot2 FastAPI + HTTPX + WebSocket 驱动 → OneBot V11 适配器 → 挂载管理 WebUI。ASGI 外层网关在 QQ 关闭时拒绝 OneBot 请求。

管理应用不处理消息，路由只调用共享服务。OneBot 适配器将消息转换为 core 标准事件；Agent 经访问规则、群聊决策、短期上下文和 LLM Provider，再由权限引擎在发送前复核动作。网页和 QQ 的模型调用经过相同的持久预算服务。

## 当前模块

- services/config_service：Pydantic Schema、原子 YAML、上一版备份与恢复。
- services/secret_service：`.env`/环境变量与 0600 JSON 密钥，API 仅提供配置存在标记。
- services/auth_service：加盐 PBKDF2、随机会话、会话摘要入库、失败登录限流。
- storage/database：SQLite 事务、schema_version、管理员、会话、向导、审计。
- core/emotion：五维全局情绪、六态状态机、时间恢复与最近 512 次变化；core/relationship：按机器人/群/用户隔离的好感度、互动和持久限额。
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

## M3 状态路径

Agent 在原有权限、过滤和预算流程中只读取当前会话关系及全局情绪，用于 system Prompt 的角色表达。成功完成直接/连续回复后才积累关系与情绪；主动插话、沉默、发送失败和管理试聊不奖励。去重只保存消息标识摘要，状态/关系表不保存聊天正文。管理员 API 使用修订号拒绝覆盖并发互动；模型没有修改这些状态的工具。schema v8 是向前追加表的迁移，升级前备份数据库；旧版程序会拒绝更高 schema。

## M4 主动私聊

QQ 私聊命令进入 ConsentStore；管理员通过受保护 API 创建并审批固定草稿。ProactiveScheduler 在 SQLite 中原子预留、检查时间窗口及硬限制，再交由 ActionExecutor 发送。发送器在速率等待后调用任务守卫复核。未确认的任务保留额度，不自动重放；NoneBot 和独立 FastAPI 生命周期负责启动/停止同一调度器。

## M4 本地日记

Agent 和主动调度器仅在平台确认发送后向 DiaryService 记录无正文活动；它按会话、自然日、许可和最低事件数生成只读事实快照与独立角色表达。独立后台任务每日处理前一日，管理 API 负责编辑/导出/删除，默认没有外部发送链路。

## M5 空间发布边界

`QZoneService` 仅接收管理员提供的公开文字/主题，不导入私聊、记忆或日记。LLM 使用既有计费链路，只返回草稿，无审批/发布工具权限；自主调度器按管理员的显式配置授权执行发布，不伪造审批。schema v13 独立保存草稿、内容摘要审批、发布预留和操作轨迹。审批绑定账号、正文、受众和配置主人，24 小时到期，编辑即失效。

`adapters/qzone.py` 只用 get_version_info / get_status / get_login_info 核实账号在线与已核实版本契约；`NoneBotTransport` 在调用 send_qzone_msg 前复核同一 Bot 对象及权限。事务预留全局 72 小时/滚动 7 天额度，发布无重试；未知或中断保留额度，只有有效 tid 显示平台确认。未核实版本禁止发布。后台约 10—11 分钟评估公开主题、在线、免打扰、费用和限额，SQLite metadata 持久租约防止重复评估；关停/修改策略后在发送前复核。默认模式需审批，可选择无需逐条审批。
