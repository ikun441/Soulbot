# API 与配置

## 认证与调用

除 bootstrap、首次创建账户和登录外，管理 API 均需有效 Cookie `soulbot_session`。写请求携带 `X-Soulbot-Request: 1`，浏览器 Origin 与 Host 一致。JSON 请求使用 `Content-Type: application/json`，上传用 multipart/form-data。

错误：401 未登录、403 来源或权限、404 不存在、409 修订冲突或重复动作、422 参数/配置、429 频率/上游限流、502 上游失败。错误不回显密钥和上游原文。编辑携带当前 revision，冲突后刷新，不盲目重试。

完整配置 Schema：`GET /api/config/schema`；完整实时接口 Schema：`GET /api/openapi.json`，均需登录。配置 PUT 使用完整 Settings，与 WebUI 相同，敏感 Key 仅写 `/api/secrets`，读取只显示是否配置。

## 新功能调用示例

| 方法与路径 | 请求 |
| --- | --- |
| POST /api/llm/tools-test | 无参数，真实验证主模型原生函数调用，成功后启用 supports_tools；计入预算 |
| POST /api/qq/retry-last | `{"confirm":true,"source_id":123}`，仅当前主人已有且未过期的私聊；省略 source_id 取最近请求，同条请求最多重试一次 |
| POST /api/image/comfyui/probe | ComfySettings；只读返回本地实际版本、checkpoint、sampler 和 scheduler |
| POST /api/search | `{"query":"公开天文科普","limit":5}` |
| POST /api/diaries/weekly | `{"day":"2026-10-20"}` |
| POST /api/diaries/1/polish | `{"request_id":"b64e254b-693a-43b2-9c91-bc8147ae9560","revision":0}` |
| POST /api/diaries/delivery | `{"day":"2026-10-20","kind":"daily"}`，kind 也可 weekly |
| POST /api/automation/stop | 无参数，保存全局暂停 |
| POST /api/backups | 返回含私人数据和密钥的 ZIP |
| POST /api/backups/restore | multipart 字段 file，校验暂存，下一次主入口启动持锁恢复 |
| DELETE /api/backups/restore | 取消等待恢复的 ZIP |
| PUT /api/personality | name、description、style、interests、prompt；保留主人 QQ 和称呼 |

润色单独调用真实模型并记入费用，同一日记修订不重复计费，生成期间修改/删除不会被覆盖。周摘要只使用同一已结束自然周、同一作用域、同一时区至少两篇归档日记。

摘要推送只创建待审批主动联系任务，不能代替真实 QQ 用户授权。仅汇总主人私聊和机器人空间活动，不包含其他群/私聊内容。自动行为暂停后恢复：读取完整配置，设 automation.paused=false，再 PUT /api/config。

## 预算预警

`GET /api/usage` 返回 `warning_only=true`、`warning_ratio`、当日 `warnings` 与持久的 `recent_warnings`。预警涵盖 daily_tokens、daily_cost、request_input、task_tokens、unknown_cost；同日同种严重程度只记录一次审计。到达阈值或费用未知均继续调用。

兼容保留旧配置名 behavior.daily_tokens / daily_cost_cny、limits.max_input_per_request / max_per_agent_task，仅作为预警阈值；behavior.budget_warning_ratio 默认为 0.8。旧响应 token_limit / cost_limit_cny 也只表示预警阈值。limits.max_output_per_request 仍控制回复长度；模型实际 context_window、工具次数及频率不属于预算开关。

## 配置字段

| 字段 | 作用 |
| --- | --- |
| web / qq | 监听、连接方向及反向 WS，变更需重启 |
| llm / tts / asr / image_generation | 独立模型和媒体 Provider，Key 不放进人格 |
| personality / emotion / relationships | 人格、连续模拟情绪和会话关系，不能授予动作权限 |
| memory.enabled / global_context | 记忆总开关及全局动作依据；只含已审核授权、有效且个人未关闭条目，保留 source_scope 与 visibility |
| memory.max_items / max_chars | 自动上下文条数/字符上限；工具分页可访问所有合格条目 |
| behavior | 群过滤、聚合、定向连续对话、冷却、免打扰和 Token/费用预警阈值 |
| behavior.split_long_replies / reply_sentences_per_message | 超过两句才分段，默认每段2句，最多5段；每段复核权限与速率 |
| image_generation.provider / comfy | openai_compatible 或 comfyui；本地固定 checkpoint 工作流，不接受任意节点输入 |
| proactive | 用户 opt-in、待审批队列、持久每日次数和随机时间窗口 |
| diary | enabled/live_preview/auto_generate/weekly_enabled、generation_time、min_events、fact_retention_days、delivery_mode(manual/daily/weekly)、delivery_bot_id |
| qzone | enabled/require_approval/autonomous_enabled、auto_bot_id/auto_audience(self/friends/public)/auto_topics；72h 间隔及7天最多2条 |
| search | SearXNG base_url/enabled/timeout_seconds/max_results/max_calls_per_minute/max_calls_per_day/price_cny_per_request；未知价不当作0，不暂停调用 |
| exploration | enabled/interval_minutes/max_daily_tasks/topics，默认关闭，只存本地 |
| automation.paused | 统一暂停自主外发、探索和自动日记/推送，不清除用户授权或重置额度 |
| tools | 工具 deny/auto/approval，模型不能伪造审批；记忆候选还须单独审核 |
| meme / emoji / html_render / appearance | 组件资源、模板限额、结构化卡片、本地壁纸 |

`.env` 优先级、反代、升级恢复与故障处理见 [debug.md](debug.md)。

## 模型工具

每任务最多 5 次工具、4 轮请求，最终最多 2 个媒体附件。create_diary 与 create_qzone_draft 每任务最多各调用一次；新工具结果均包含服务端保存回执。所有参数经过 Schema 校验；会话身份由服务端提供，模型不能指定他人身份。生成不等于发送，动作仍须权限与预算复核。

| 工具 | 用途 |
| --- | --- |
| discover_tools | `names` 每次最多3个；只从服务端为当前会话提供的工具中载入参数说明，不能授予权限或执行动作 |
| create_diary | `day` 可选，省略为今天；会话取自服务端，只汇总本会话确认活动，不接受正文或其他身份 |
| create_qzone_draft | `content`；仅主人私聊，账号和受众取服务端，保存 model_chat_draft 来源，不调用发布接口 |
| search_web | 公开搜索，不下载结果地址，不执行网页指令 |
| retrieve_memory / browse_memory | query、offset；全局模式查询所有已审核有效授权记忆并标注来源，关闭全局后限定当前会话 |
| save_memory | content、importance；仅保存待审核且未授予长期权限的候选 |
| search_emoji / browse_emoji / select_emoji | 有界检索/分页/ID 选择，禁止任意文件路径 |
| list_meme_templates / get_meme_info / generate_meme | 查询实际模板和元数据，验证数量/参数与资源 ID |
| render_html | 固定卡片与结构化内容，禁脚本、网络和任意 HTML |
| synthesize_speech / generate_image | 独立服务配置、预算和媒体作用域，发送前再次检查 |

其他模型任务也会获得有界全局记忆参考；声明支持原生工具调用时，还可在动作内部用只读记忆检索分页。模型不得从记忆文本获得额外权限，不能直接执行 Shell、任意文件操作或更改系统设置。

## 全部实际路由

| 方法 | 路径 |
| --- | --- |
| GET | `/` |
| POST | `/api/asr/action` |
| GET | `/api/asr/status` |
| POST | `/api/asr/transcribe` |
| POST | `/api/audio/send-test` |
| GET | `/api/audio/{key}` |
| GET | `/api/audit` |
| GET | `/api/auth/account` |
| PUT | `/api/auth/account` |
| POST | `/api/auth/login` |
| POST | `/api/auth/logout` |
| POST | `/api/auth/setup` |
| POST | `/api/automation/stop` |
| POST | `/api/backups` |
| DELETE | `/api/backups/restore` |
| POST | `/api/backups/restore` |
| GET | `/api/backups/status` |
| GET | `/api/bootstrap` |
| POST | `/api/chat` |
| GET | `/api/config` |
| PUT | `/api/config` |
| GET | `/api/config/export` |
| POST | `/api/config/restore` |
| GET | `/api/config/schema` |
| DELETE | `/api/context` |
| GET | `/api/diaries` |
| POST | `/api/diaries/delivery` |
| POST | `/api/diaries/generate` |
| GET | `/api/diaries/status` |
| GET | `/api/diaries/weekly` |
| POST | `/api/diaries/weekly` |
| DELETE | `/api/diaries/weekly/{key}` |
| DELETE | `/api/diaries/{key}` |
| GET | `/api/diaries/{key}` |
| PUT | `/api/diaries/{key}` |
| GET | `/api/diaries/{key}/export` |
| POST | `/api/diaries/{key}/polish` |
| GET | `/api/emoji` |
| POST | `/api/emoji/refresh` |
| POST | `/api/emoji/upload` |
| GET | `/api/emoji/{key}/image` |
| PUT | `/api/emoji/{key}/labels` |
| GET | `/api/emotion` |
| PUT | `/api/emotion` |
| POST | `/api/emotion/reset` |
| GET | `/api/exploration` |
| DELETE | `/api/exploration/{key}` |
| GET | `/api/health` |
| POST | `/api/image/comfyui/probe` |
| POST | `/api/image/generate` |
| GET | `/api/image/status` |
| POST | `/api/llm/models` |
| POST | `/api/llm/test` |
| POST | `/api/llm/tools-test` |
| POST | `/api/media/send-test` |
| POST | `/api/media/upload` |
| GET | `/api/media/{key}` |
| POST | `/api/meme/action` |
| GET | `/api/meme/extensions` |
| POST | `/api/meme/extensions/{source}/install` |
| POST | `/api/meme/generate` |
| GET | `/api/meme/status` |
| GET | `/api/meme/templates` |
| GET | `/api/meme/templates/{key}` |
| POST | `/api/meme/templates/{key}/preview` |
| POST | `/api/meme/test` |
| GET | `/api/memories` |
| POST | `/api/memories` |
| POST | `/api/memories/preview` |
| DELETE | `/api/memories/{key}` |
| PUT | `/api/memories/{key}` |
| PUT | `/api/personality` |
| GET | `/api/personality/export` |
| GET | `/api/personality/preview` |
| GET | `/api/personality/templates` |
| GET | `/api/proactive/contacts` |
| PUT | `/api/proactive/contacts/{key}` |
| GET | `/api/proactive/jobs` |
| POST | `/api/proactive/jobs` |
| POST | `/api/proactive/jobs/{key}/review` |
| GET | `/api/providers/status` |
| POST | `/api/qq/retry-last` |
| POST | `/api/qq/send-test` |
| POST | `/api/qq/test` |
| POST | `/api/qzone/capability` |
| GET | `/api/qzone/drafts` |
| POST | `/api/qzone/drafts` |
| GET | `/api/qzone/drafts/{key}` |
| PUT | `/api/qzone/drafts/{key}` |
| POST | `/api/qzone/drafts/{key}/publish` |
| POST | `/api/qzone/drafts/{key}/review` |
| GET | `/api/qzone/status` |
| GET | `/api/relationships` |
| POST | `/api/relationships` |
| DELETE | `/api/relationships/{key}` |
| PUT | `/api/relationships/{key}` |
| POST | `/api/render/action` |
| POST | `/api/render/generate` |
| GET | `/api/render/status` |
| POST | `/api/search` |
| PUT | `/api/secrets` |
| GET | `/api/tool-approvals` |
| POST | `/api/tool-approvals/{key}/review` |
| GET | `/api/tools` |
| GET | `/api/tts/status` |
| POST | `/api/tts/synthesize` |
| GET | `/api/tts/voices` |
| GET | `/api/usage` |
| POST | `/api/wallpaper` |
| GET | `/api/wallpaper/{name}` |
| POST | `/api/wizard/complete` |
| PUT | `/api/wizard/progress` |


## v2.0.3 设置、记忆与插件

所有以下接口要求管理员会话，并沿用现有来源验证。设置通过 `/api/config` 的统一 Schema 保存；`action_memory` 默认开启局部与全局检索，`reflection` 默认关闭。全局检索只包含审核通过、获长期保存授权、有效且未关闭的记忆，不赋予外发权限。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/reflection/status` | 后台候选任务、动作记忆访问计数 |
| POST | `/api/reflection/run` | 唤醒已有候选队列，不伪造输入事件 |
| GET / POST | `/api/emoji/categories` | 分类列表 / 创建分类 |
| GET | `/api/emoji/status` | 索引与监测状态 |
| POST | `/api/onboarding/emoji/prepare` | 初始化及刷新内置表情库 |
| GET | `/api/plugins` | 已安装插件、脱敏参数、市场和安装状态 |
| POST | `/api/plugins/market/refresh` | 读取指定仓库固定提交的市场索引 |
| POST | `/api/plugins/install` | 按 id、commit 安装；须 acknowledge_download=true |
| PUT | `/api/plugins/{id}/config` | revision、values、secrets、clear_secrets、policy |
| POST | `/api/plugins/{id}/enable` | revision 及 acknowledge_python_execution=true |
| POST | `/api/plugins/{id}/disable` | revision，停用插件 |

Emoji 浏览与搜索支持 `category` 参数，模型工具包含 `list_emoji_categories` 和按分类分页的 `browse_emoji`。记忆审核可提交 `replace_target`，仅在来源作用域与目标 revision 一致时替换。

插件元数据使用 `define.name`、`define.author`、`define.version`、`define.info`；工具经 Tool Registry 权限与参数验证，默认需要审批。GitHub 私有源凭据通过 `/api/secrets` 的 `plugin_github_token` 写入，普通读取只返回配置状态。完整 SDK 与市场索引格式见[插件开发规范](https://github.com/ikun441/soulbot_plugins/blob/main/PLUGIN_SPEC.md)。
