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
| personality / emotion / relationships | 人格、连续模拟情绪和全局用户关系，不能授予动作权限 |
| memory.enabled / global_context | 记忆总开关及全局动作依据；只含已审核授权、有效且个人未关闭条目，保留 source_scope 与 visibility |
| memory.max_items / max_chars | 自动上下文条数/字符上限；工具分页可访问所有合格条目 |
| behavior | 群过滤、聚合、定向连续对话、冷却、免打扰和 Token/费用预警阈值 |
| behavior.split_long_replies / reply_sentences_per_message | 超过两句才分段，默认每段2句，最多5段；每段复核权限与速率 |
| image_generation.provider / comfy | openai_compatible 或 comfyui；本地固定 checkpoint 工作流，不接受任意节点输入 |
| proactive | 用户 opt-in、待审批队列、持久每日次数和随机时间窗口 |
| diary | enabled/live_preview/auto_generate/weekly_enabled、generation_time、min_events、fact_retention_days、delivery_mode(manual/daily/weekly)、delivery_bot_id |
| qzone | enabled/require_approval/autonomous_enabled、auto_bot_id/auto_audience(self/friends/public)/auto_topics/auto_image(none/emoji/generated)；72h 间隔及7天最多2条 |
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
| create_qzone_draft | `content`、可选 `images:[{reference}]`（最多3张）；仅主人私聊，账号和受众取服务端，保存 model_chat_draft 来源，不调用发布接口 |
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
| POST | `/api/llm/vision-test` |
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


## v2.0.4 管理操作、共享记忆与频率

`GET /api/plugins` 的插件条目新增 `admin_actions`（名称、标题、说明、参数 JSON Schema 与 mutating）；`POST /api/plugins/{id}/actions/{name}` 接收 `{revision, values, confirmed}`，需要管理员会话，写操作 confirmed=true。此接口不向模型开放，不接受任意命令名或 Shell。

统一配置新增 `memory.shared=true`、`memory.model_auto_save=true`、`memory.max_model_writes_per_day=20`。记忆仍须启用总开关；来源关闭和删除继续有效。模型 save_memory 参数为 content、kind、importance、confidence、expires_in_days；输出 memory_id、status、active，只有 active 才可声称已生效，不能通过模型参数自行更改审核策略。

频率新增 `behavior.passive_reply_probability=1`、`always_reply_mentions=true`、`natural_candidate_probability=0.08`、`active_candidate_probability=0.20`。原 `reply_cooldown_seconds`、`proactive_interval_seconds` 与 `max_proactive_per_day` 继续保存到同一 Schema。GroupPolicy 可用相同冷却/每日字段和 passive_reply_probability、proactive_probability 覆盖默认值，null 继承，0 是有效设置。

模型 generate_meme 的 images 增加 avatar:bot、avatar:user、avatar:mentioned，只能由真实 QQ 上下文解释。QQ 命令与模型调用均复用模板校验、工具权限和生成速率；生成图片与平台发送回执分开。


`experience_sampling` 配置包含 enabled（默认 true）、memory_items（6）、chat_items（8）、max_chars（4000）、chat_hours（24）。日记按指定日期和原时区取样，空间按近期窗口取样。`diary.ai_expression=true` 表示对前一日已归档的模板日记按原润色去重规则自动生成表达，不自动发送。只从仍保留且来源未关闭的聊天读取，不复制原始聊天到新表。


## v2.0.5 身份与记忆更新

GET `/api/identities?query=` 为管理员提供账号、平台昵称、群名片、群、称呼和好感度映射。主人身份按 QQ 号匹配，平台昵称和名片不授予权限。SQLite 增量迁移至 v18。

`update_memory` 参数为 memory_id、revision 及可选 content、kind、importance、confidence、expires_in_days。身份由服务端上下文确定，普通用户限本人来源，主人可跨来源编辑；旧 revision 拒绝覆盖。默认回复冷却 0 秒。


## v2.1.0 全局关系、收图与上传分类

关系以 `(bot_id,user_id)` 唯一，全群与私聊共享好感度、称呼、计数、冻结与限频。创建接口旧 `group_id` 参数为兼容保留，验证格式后忽略、统一保存为空；已有用户重复创建返回409，应编辑原记录。增量迁移至 schema v19，旧行留在 `relationships_before_global` 迁移快照表，仅用于运维检查，不参与检索；完整回退使用升级前备份。

`llm.supports_vision` / `llm.fallbacks.<id>.supports_vision` 声明 `image_url` 能力。`vision.enabled=false` 默认禁用 QQ 收图，`vision.max_images=3` 范围1—4，`vision.max_side=1024` 范围256—1536，均热更新。POST `/api/llm/vision-test` 无参数、需管理员登录及请求标识：对当前主模型发送系统随机色块图，正确识别且配置未变化才保存主模型视觉声明，不替管理员打开 QQ 收图开关。失败如实返回 ProviderError，不虚报已启用。

收图路径仅接受消息附件的 QQ 图片域名，拒绝任意网址、本地路径与HTTP重定向；过期URL通过NapCat `get_image(file_id=...)` 刷新，忽略返回的本地文件路径。最多2批并发，每批30秒总超时，按PNG/JPEG/GIF/WebP实际内容校验，缩小并转JPEG去元数据后以 `detail=low` 输入兼容 Chat Completions 接口。图片仅当轮使用；后续历史只保留文本和图片状态说明。不开视觉或读取失败的纯图消息直接提示，不请求模型编造。

表情上传使用原生下拉选择分类，分页读取完整分类列表、保留选择，新增分类自动选中。POST `/api/emoji/upload` 的 multipart `category` 长度上限120，支持合法已存在子分类；新增普通分类继续使用分类管理接口。拒绝隐藏路径、目录跳转、符号链接和不存在的子分类。

上游契约核对：[NapCat 文件处理](https://napneko.github.io/develop/file)、[DeepSeek Chat Completions 图片输入](https://api-docs.deepseek.com/api/create-chat-completion/)。不根据模型名字猜测能力，视觉与工具能力按实际配置路由。


## v2.1.1 推理模型与结束原因

`llm.reasoning_effort` 和 `llm.fallbacks.<id>.reasoning_effort` 可选 `provider`（默认，不传参数）、`none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`。管理员须按对应模型支持情况选择；错误取值不会伪装成能力支持。可在模型页保存，立即影响后续请求。DeepSeek参数依据：[官方Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。

普通及流式响应 `finish_reason=length` 返回 `output_limit`，`content_filter` 返回同名错误，`aborted/insufficient_system_resource` 返回 `generation_interrupted`。不返回截断的工具参数供执行。普通响应在发送文本前检查，流式试聊可能已显示部分文字，但最终明确失败，不能将其视为完整回答。错误通过现有502响应返回 `detail/kind/uncertain`，不返回推理正文。

有有效usage的上述失败以及流式中断，`llm_usage`保存 `status=failed`、真实输入/输出/推理Token、estimated=0及可计算费用。缺少usage时仍为uncertain并保留估算；费用未知不记为免费。每次尝试计入任务统计，截断不会在同服务自动重试。

连接、工具及视觉验证各自的输出请求上限为512，最终由实例单次输出上限、模型上下文共同裁剪，不绕过已配置上限。视觉验证通过只保存 `supports_vision=true`，不会自动打开 `vision.enabled`。


## v2.2.0 主动媒体与账号名录

POST `/api/proactive/jobs` 新增 `attachments:[{"reference":"media:<id>"}]`，至多2个。引用支持emoji/media/audio的32位素材ID，必须为管理端可读素材；拒绝任意网址或文件路径。content可为空，但文字与附件至少一项。保存时复制并记录校验和，收件人、内容和附件不可变；相同request_id重试不重复创建，文字旧客户端的幂等请求保持兼容。

任务返回attachments的kind/mime/size/预览URL及delivered_parts（已完整确认的文字或附件部分数，不是QQ消息条数；合成语音标识单独发送）。GET `/api/proactive/jobs/{id}/attachments/{index}` 仅管理员可访问，禁止缓存。通过原review接口审批，派送继续检查QQ私聊授权、免打扰、窗口、次数和每类媒体权限。部分成功、超时或中断统一保留消耗并标未确认，不自动重试；附件内容变化时停止派送。文字任务保留90秒租约，媒体任务180秒租约、120秒派送超时。

`proactive.auto_media` 可选none（默认）、emoji、voice，仅在auto_drafts及授权策略允许时生成待审批素材。emoji仅能选服务端提供的候选ID，voice使用已配置TTS并计入费用。所有AI候选仍需管理员审批，局部/全局记忆按统一配置检索，关系与身份基于真实收件人。

GET `/api/identities?query=&offset=0&limit=50` 支持QQ、昵称、备注、群名片和历史名称检索，limit最多100。is_friend为1/0/null，分别表示最近一次完整好友同步时是好友/不是好友/尚未核实；friends_synced_at表示核实时刻。好友关系和称呼不授予系统权限。GET `/api/identities/{bot}/{user}/history` 返回名称历史；DELETE同路径加`/{id}?last_seen=<timestamp>`删除单条，更新冲突返回409。删除历史不修改平台当前名称，下次平台观察可能重新记录。

POST `/api/identity-sync` 需管理员，参数request_id（UUID）、bot_id、include_friends=true、include_members=true、group_ids=[]、max_groups=50（1—200）。只允许已连接机器人，指定群须属于实际返回的已加入群；群名单生效。后台只调用get_friend_list/get_group_list/get_group_member_list，15秒单请求、600秒任务总超时。GET同路径查询进度；POST `/api/identity-sync/cancel`取消。状态idle/running/completed/partial/failed/cancelled/interrupted。断点记录保留但不自动重放；单群失败继续其他群，部分成员列表不删除旧观察；取消等待正在写入的批次回滚或完成。同步不会新增好感度记录或主动联系授权。

SQLite迁移至v20，新增附件/派送进度、好友备注与核验状态、名称历史。完整备份包括私有固定附件并检查待发送任务引用完整性；老代码回退须恢复升级前备份。NapCat只读契约：[官方API文档](https://napneko.github.io/onebot/api)。


## v2.3.0 空间配图

POST `/api/qzone/drafts` 接受 `images:[{"reference":"media:<32位ID>"}]` 或 `emoji:<32位ID>`，最多3张，默认空。管理员先通过 `/api/media/upload` 上传；只能用不透明引用，不接受网址或文件路径。重复 request_id 的相同请求返回同一固定草稿，不重新读取已过期源素材。

PUT `/api/qzone/drafts/{key}` 中 images 省略/null 表示保留、[] 表示移除，非空则整体替换。正文、受众或图片修改均递增 revision 并使审批失效。返回 images 仅含受保护预览URL、mime、宽高与大小，不暴露物理路径或内部哈希。GET `/api/qzone/drafts/{key}/images/{index}?revision=N` 需要管理员会话；旧版本返回409，不存在返回404，图片缺失/变化返回422，响应禁止缓存。

原图最多16MB，解码像素限制沿用媒体服务；固定副本转为去EXIF的JPEG，最长边2048px、每图最多4MB，透明区域合成白底。原素材过期不影响草稿。发布及审批重新校验副本；修改、缺失、未确认图片能力时不占发布额度。已提交到平台的超时/中断保留额度，状态uncertain，不自动重试。

能力探测新增 images_supported，仅已核实的NapCat 4.18.38契约声明支持。实际发送采用 send_qzone_msg 的 base64:// 图片数组及 ugc_right，以有效tid确认成功。[官方契约](https://github.com/NapNeko/NapCatQQ/blob/v4.18.38/packages/napcat-onebot/action/extends/SendQzoneMsg.ts)。

`qzone.auto_image` 默认为none，可设emoji或generated。后者只向图片Provider发送配置的公开主题，不上传聊天、记忆采样；调用前检索动作记忆，准备及发送前复核权限。关闭原有审批的用户配置继续生效，不伪造人工审批。模型create_qzone_draft可引用同会话生成图片/卡片及本地表情；拒绝聊天上传照片、头像和其他会话素材，只保存不发布。

完整备份纳入data/qzone_images固定副本，校验待发布附件完整性；已结束草稿图片保留30天、无引用副本保留24小时后清理，uncertain及待处理草稿不自动清理。数据库追加迁移至v21，旧纯文字草稿与幂等请求保持兼容。


## v2.4.0 搜索备用路由

search保留原有enabled/base_url/timeout_seconds/price_cny_per_request/max_results及频率字段，新增total_timeout_seconds（默认30，2–90秒）和fallbacks（最多2项，键fallback_1/fallback_2）。每项包含enabled（默认false）、base_url、timeout_seconds（2–30秒）和独立price_cny_per_request（未知为null）。启用地址按协议、主机、端口和路径去重，不接受凭据URL；目前均为SearXNG能力，不能当作其他Provider的替代。

POST `/api/search` 仍接收query/limit。有效响应新增provider_id（primary/fallback_1/fallback_2）及attempts（provider/status/error_kind）。主服务成功或合法空结果即停止；连接、超时、限流、认证或上游不可用等错误可切换不同服务，不重试同一地址。HTTP400/422参数错误、取消、权限/配置变化、调用次数限制不继续切换。每次备用尝试同样发送搜索词，可能额外计费；所有失败保守保留费用，未知报价不作为零成本。

GET `/api/search/providers` 返回各已保存端点的最近真实测试状态及15条请求尝试。编辑配置使对应状态失效，禁用后显示disabled；不发起网络请求。POST `/api/search/providers/{name}/test` 需要管理员会话、来源校验及严格JSON `{"confirm":true}`，只向所选已启用端点发送固定公开测试词，不使用备用服务。未知端点404，未启用或配置变化503，失败按Provider错误分类。

WebUI离开搜索页面会中止等待；服务端检测断开后取消当前任务，不继续备用调用，已发出的尝试保留uncertain记录。工具自身或调用方的更短超时仍优先生效。服务关闭取消正在运行的搜索。

SQLite v22追加search_attempts(usage_id/request_id/provider/error_kind)，与原search_usage逐项关联。search_usage保持原6列以兼容旧数据；旧记录在查询中显示primary。分钟/每日搜索上限按实际尝试计数，探索每日任务按request_id去重，备用请求不重复占探索任务数。备份恢复合并新增明细及原用量，不能通过恢复退回费用或调用额度。`/api/usage` 中search.recent新增provider/request_id/error_kind。

接口契约依据：[SearXNG 官方 Search API](https://docs.searxng.org/dev/search_api.html)，使用GET /search及q/format=json/safesearch参数，结果只作为不可信资料，不下载结果链接。


## v2.5.0 TTS/ASR 备用路由

`tts.fallbacks` 与 `asr.fallbacks` 分别支持 fallback_1/fallback_2。每项有 enabled、provider、timeout_seconds 及对应服务字段，默认空。TTS支持edge或openai_compatible，edge及compatible配置与主服务同结构；ASR支持local或openai_compatible，base_url/model/language/price_cny_per_minute与主服务同结构。拒绝同一能力内重复的启用端点，TTS允许同服务的不同音色；ASR不重复使用同一本地模型。

`tts.total_timeout_seconds` 默认90（5–240秒），`asr.total_timeout_seconds` 默认180（5–360秒）；每次尝试受独立timeout和剩余总时长共同约束，工具/调用方更短的超时仍优先。输入不合法、输出大小/时长超限、取消、配置/密钥变化、权限关闭和全局暂停自动任务时不切换；已有合法音频或ASR空结果即停止。缺少配置/本地未准备的服务不会预留调用额度；实际失败与备用调用分别记账。

备用独立密钥名为 tts_fallback_1/tts_fallback_2/asr_fallback_1/asr_fallback_2，环境变量为 SOULBOT_TTS_FALLBACK_1/2、SOULBOT_ASR_FALLBACK_1/2。与原密钥使用同一受保护写入API、读取配置仅返回是否已配置，导出默认脱敏。不会复用主服务、聊天或其他类型密钥。

`asr.allow_cloud_fallback` 默认false，启用任一云端备用前须显式设true。关闭授权时WebUI同时取消云端备用启用项；本地失败本身不构成上传授权。主服务主动选择云端与此备用授权分别管理。已发送请求无法撤回，取消后可能已上传/计费。本地备用不自动下载模型。

GET `/api/tts/status`、`/api/asr/status` 新增 routes 数组，含 id/provider/state/last_test_at/last_error_kind；现有provider_test表示整条路由最近结果，端点或密钥改变使匹配状态失效。TTS POST `/api/tts/synthesize` 新增可选provider_id：auto（默认）、primary、fallback_1、fallback_2。ASR POST `/api/asr/transcribe` 用同名查询参数。指定单个服务只测试该服务，不自动切换；服务需已启用并配置。

TTS返回产物新增provider_id，ASR转写结果新增provider_id及实际provider（local/openai_compatible）；文本/录音及转写结果不进入路由账本。GET `/api/tts/voices?provider_id=primary|fallback_1|fallback_2` 可只读查询对应Edge音色，已保存但未启用的备用也可查询；兼容接口不虚构音色列表。

SQLite v23追加media_attempts(usage_id/request_id/capability/route_id)，原tts_usage/asr_usage列布局保持兼容，`/api/usage` 对应recent新增route_id/request_id，旧条目视为primary。完整备份合并新增明细和费用/频率，不通过恢复退回已消耗额度。每个实际尝试计次及字符/录音秒数，未知费用不显示零，已派发失败/取消按错误可确认程度结算。


### 原生页面资源版本

页面入口、CSS/Logo及ES Module依赖URL带当前版本参数。发布时先运行 `scripts/web_assets.py` 同步，`scripts/release_check.py` 会检查所有版本参数，避免只更新入口而保留旧子模块。根页面与/static/响应使用Cache-Control:no-cache，允许ETag条件验证；私人API继续no-store，CSP仍限制同源脚本。升级后刷新页面即可加载新模块，已打开旧页面需刷新后再使用新字段。


## v2.6.0 生图备用路由

`image_generation.fallbacks` 支持fallback_1/fallback_2，每项使用原生图端点字段：enabled/provider/comfy/base_url/model/size/quality/response_format/output_format/timeout_seconds/price_cny_per_image。仅支持openai_compatible与comfyui，不接受html_render；拒绝重复的启用地址与模型组合。ComfyUI继续只允许本机回环HTTP、已验证基础节点与固定单张工作流。

备用密钥名image_fallback_1/2，对应环境变量SOULBOT_IMAGE_FALLBACK_1/2；受保护配置写入独立保存、读取只返回是否配置，不复用其他能力或主服务密钥。

`total_timeout_seconds`默认240（5–1200秒），每次尝试同时受该总时限与自身timeout约束；工具调用方更短的超时继续优先。`fallback_on_uncertain`默认false，已提交或费用/结果不确定时停止，开启后才允许继续相应可切换错误，可能重复生成/计费。连接未建立或ComfyUI只读探测失败属于未提交，可直接尝试备用；参数、大小、仅URL返回、取消、限额、权限或配置变化不继续切换。

GET `/api/image/status` 新增routes，POST `/api/image/generate` 在prompt之外接受provider_id=auto（默认）/primary/fallback_1/fallback_2。指定一个服务不自动切换；返回产物包含实际provider_id/provider。POST `/api/image/comfyui/probe` 可传选中备用的ComfySettings读取实际模型/节点，不提交生图。费用记录image_generation.recent新增route_id/request_id。

自动空间生图沿用路由并复核策略，租约覆盖整条路由的最大时长；模型工具说明考虑启用的ComfyUI候选语言配置，不在服务端擅自改写或翻译用户手动提示词。

SQLite v24重建media_attempts的能力约束，纳入image_generation并保留原tts/asr行、主键和请求索引。备份合并新旧尝试与用量，不退回费用/额度。ComfyUI取消只向/queue提交本任务ID删除，不调用全局/interrupt；已运行或未知ID任务不保证被取消，不会宣称服务端已停止。
