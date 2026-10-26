# 配置说明 · v0.9.5

普通用户通过 WebUI 配置，不需要手改 YAML。首次启动自动生成 config.yaml，实际字段以 `Settings.model_json_schema()` 为准，登录后可访问 `/api/config/schema`。v0.3.0 会把旧数据库迁移到 schema v4，保留账户、额度和上下文，并增加表情索引、图片产物及会话范围；升级前按启动文档备份。

| 配置区 | 当前字段 |
| --- | --- |
| soulbot | name、language、timezone |
| web | host、port（重启生效） |
| qq | enabled、connection、test_url、reverse_ws_url（反向地址保存到 .env，重启生效） |
| llm | OpenAI兼容 base_url/model、重试、流式、显式能力、备用服务、价格 |
| personality | description、style、interests、owner_qq、owner_title、prompt |
| emotion | enabled、automatic、五维 baseline、恢复半衰期、互动冷却、高低阈值、滞回宽度与最短状态停留时间 |
| relationships | enabled、automatic、每次好感增加值、奖励冷却与每条关系每日增长上限 |
| behavior | 群聊模式、过滤/聚合/冷却、群级策略、免打扰、上下文、每日 Token/费用限制；autonomous/diary/qzone 当前必须 false |
| appearance | motion、wallpaper、wallpaper_kind |
| limits | 每次输入/输出、每个 Agent 任务总 Token；工具 Schema 和所有模型轮次均计入 |
| emoji | enabled、directory（固定 ./emoji）、recursive、max_candidates、browse_enabled、max_files、max_filename_chars |
| meme | enabled（默认 false）、managed、auto_install、version（固定0.1.14）、base_url、timeout_seconds、max_restarts |
| tools | 媒体工具的 deny/auto/approval；当前 approval 会拒绝，审批队列尚未开放 |
| html_render | enabled（默认 false）、width（480—1200）、max_height（400—2400）、timeout_seconds（5—60）；浏览器由卡片工坊明确准备 |

未知字段、无效端口/时区/非有限费用、非受支持的壁纸路径均被拒绝。失败不会覆盖现有文件。每次保存自动备份上一版至 data/config.backup.yaml；恢复操作会交换当前与备份版本。

密钥来源优先级：进程环境变量 → 项目 .env → data/secrets.json。变量为 `SOULBOT_LLM_API_KEY`、`SOULBOT_LLM_FALLBACK_1`、`SOULBOT_LLM_FALLBACK_2`、`SOULBOT_ONEBOT_ACCESS_TOKEN`。WebUI 输入写入 data/secrets.json（0600）；设置了环境变量时请在环境来源修改，WebUI 保存不会覆盖上游环境。

WebUI 账号使用 `.env` 中的 `SOULBOT_WEBUI_USERNAME`、`SOULBOT_WEBUI_PASSWORD_HASH`；可填 `SOULBOT_WEBUI_PASSWORD` 作为启动时输入，随后自动转换并删除明文。反向地址使用 `SOULBOT_ONEBOT_REVERSE_WS_URL`，优先级为进程环境 → .env → YAML。创建/修改账号和编辑反向地址均可通过 WebUI 完成；更改账号立即生效，接收路径需重启。参见 [.env 配置说明](environment.md)。

QQ 必须有 Token 才能启用。Token 修改需要重启。密钥不会出现在 YAML、GET 配置或导出中。导出还会清空主人 QQ，但不自动审查用户填写的 Prompt，请自行检查。

备份/数据库均为私密文件。完整备份应停止程序并复制 config.yaml、data 和实际 .env，详见 start.md。

## 好感与模拟情绪

规则经相同 Pydantic Schema 保存到 YAML 并热更新。当前情绪、关系分数、称呼、修订号、冷却与每日增长存在 SQLite，配置导出不包含这些运行数据。管理页面可编辑当前数值、冻结自动变化和重置情绪；修改规则不清空状态。详见 [M3 状态说明](social-state.md)。v0.9.5 自动迁移到 schema v8；升级前备份，回退程序需恢复兼容版本的备份。

## 语音设置
`tts` 与聊天模型配置独立，默认关闭。密钥使用 `SOULBOT_TTS_API_KEY`，普通读取仅返回配置状态。音色、语速、字符/调用上限和兼容服务参数均可在语音工坊热更新，详见 [TTS 说明](tts.md)。

## 语音识别
`asr.enabled` 与 `asr.qq_enabled` 默认均关闭。`SOULBOT_ASR_API_KEY` 为独立识别密钥；本地模式无需密钥，但需在 WebUI 准备模型。最多 8MB 的实际 MP3/PCM WAV。热更新识别设置，QQ 连接方式变化仍需重启；QQ ASR 当前要求反向 WebSocket。详见 [ASR 说明](asr.md)。

## 云端生图
`image_generation` 默认关闭，使用独立 `SOULBOT_IMAGE_API_KEY`。可选参数留空/omit 不发送，按服务实际能力选填；每次一张，报价按当前模型/尺寸/质量确认。频率和每日图片额度持久化，费用与其它 Provider 共享。详见 [生图说明](image-generation.md)。
