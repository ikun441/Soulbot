# 排查、运维与验收

## 先判断哪一层失败

| 现象 | 检查 |
| --- | --- |
| 启动失败 | Python 3.11–3.13、目录权限、网络与终端错误；所有依赖在 .venv，勿全局 sudo 安装 |
| WebUI 打不开 | 终端仍运行，URL 用 http，端口以启动输出为准；用 lsof/netstat 查看占用，勿直接杀死其他服务 |
| 提示实例已运行 | 同一个 SOULBOT_HOME 只允许运行一个进程，先停止旧实例 |
| QQ 未连接 | NapCat 扫码在线、反向 WS 客户端启用、地址方向正确、端口和 Token 一致；更改后重启 SoulBot |
| 已连接但无回复 | 检查私聊/群策略、群名单、@/唤醒词、静音、冷却、QQ 权限、模型服务余额与连接；SoulBot 预算仅预警，去重消息不会重发 |
| 模型失败 | Base URL、实际模型 ID、Key、声明能力；401/403 查权限，429 查限额，超时/网络异常先查用量，避免反复计费 |
| 日记为空 | 开启并保存，至少达到每会话 min_events 条平台确认事件；管理命令/试聊不计入；可刷新当天预览，不能凭空补写历史 |
| 周摘要为空 | 选择已结束的自然周，该周同一会话至少两篇归档日记；不同用户或时区不拼接 |
| 搜索 403 | SearXNG 必须启用 JSON 格式；公共实例常禁用 API，不能把 HTML 页面当 JSON 结果 |
| 自动行为未执行 | 查看全局暂停、各功能开关、夜间免打扰、用户授权、连接、持久频率；费用未知仅预警，授权与限频不会因重启被跳过 |
| 空间草稿不能发布 | 账号离线、版本未核实、隐私标记、审批失效、72 小时或 7 天额度未到；uncertain 必须人工查看 QQ 空间，不自动重试 |
| 表情/卡片失败 | 在对应页面检查托管安装、素材、字体、Chromium 和第三方端口；停止/重试不会影响核心服务 |
| ASR/TTS/生图失败 | 独立配置和密钥，服务商声明的模型/格式；云端请求可能计费，ASR 会上传录音；Edge 为非官方服务，可能限流 |

操作记录在 WebUI“操作记录”，仅记录安全事件和类别，不记录聊天正文或上游密钥。日志位于 logs；不要把 .env、数据、导出的私人 JSON 或完整备份发到公共 issue。

## 配置与凭据

配置来源为非敏感 `config.yaml`、`.env`/`data/secrets.json` 和运行 SQLite。Pydantic Schema 由受保护的 `/api/config/schema` 提供。配置保存原子写入并保留上一版；QQ、监听、Token 等变更需重启，其他设置热更新。

```dotenv
SOULBOT_WEBUI_USERNAME=my_admin
SOULBOT_WEBUI_PASSWORD='替换为至少12位的独立密码'
SOULBOT_ONEBOT_REVERSE_WS_URL=ws://127.0.0.1:2526/onebot/v11/ws
```

用户名为 3–40 位字母、数字、下划线或短横线；口令 12–128 位，账号与口令成对配置。启动转换为带随机盐的 PBKDF2 哈希，删除明文 PASSWORD 行，保存 PASSWORD_HASH；修改口令退出其他会话。可用此方式恢复密码，不需要删除数据库。

优先级：进程环境 → 项目 .env → YAML/密钥文件。`${...}` 不做插值。进程管理的账号和反向地址不能被 WebUI 覆盖。`.env` 与密钥文件 POSIX 权限 0600。

`SOULBOT_HOME` 可指定独立实例目录，默认仓库根目录。`SOULBOT_PYTHON` 指定启动解释器。域名访问在进程 `SOULBOT_ALLOWED_HOSTS` 添加允许的主机名，勿使用通配以绕过校验。

## 反向代理与部署

单进程启动；不配置多个 Uvicorn worker，否则 QQ 连接和调度可能重复。反代支持自定义 WS 接收路径；代理需转发 Upgrade、Authorization、X-Self-ID，wss 的 TLS 由代理终止。外网或非回环监听必须设置一致的 OneBot Token。WebUI 使用 HttpOnly/SameSite 管理会话、来源校验和 CSP；HTTPS 部署由代理配置。

默认使用 16GB 机器，不以 2GB 为设计约束。本地 Whisper、Chromium 与 meme-generator 是可选组件；核心故障与组件状态分离。Linux/Windows 的最终实机状态以 CI 和真实运行报告为准，不把静态兼容代码当作已实测。

## 备份、恢复及迁移

“系统与备份”生成 SQLite backup API 的一致快照，包含提交的 WAL、配置、.env、密钥、Emoji 和壁纸。排除虚拟环境、第三方下载、日志、临时媒体及旧备份，最大 256MB。备份含私人数据且未加密，请用自己的加密存储保管。

上传 ZIP 只校验和暂存，不在运行中替换数据库。下次主入口取得实例锁后应用，自动保存 `data/backups/before-restore-*.zip`，撤销旧登录会话，暂停自主行为。恢复合并现有费用、发送额度及冷却，避免旧快照退回限额；仍须用备份账号登录并核对配置。暂存可在界面取消。恢复失败会保留旧数据和恢复前快照，先检查终端错误再操作。

ZIP 文件名单和 SHA-256 校验受控，禁止路径穿越、重复路径、符号链接、未知文件及超大解压。恢复前必须停机持锁。数据库 schema v15，迁移逐版追加、保留已有数据；高版本数据库拒绝旧程序启动。降级应使用对应版本代码与匹配的完整备份，勿只改 schema_version。

手动紧急备份应在停止 SoulBot 后复制 `.env`、`config.yaml`、`data` 与 `emoji`，保留原目录权限；不要在运行中只复制 soulbot.db 而忽略 WAL。

## 行为与记忆边界

- 群普通消息先规则过滤，再进行候选概率、冷却与权限检查，并记录预算预警；90 秒窗口仍需明确指向机器人。
- 主动私聊只接受用户从 QQ 私聊授权；管理员或模型不能代为授权。撤销/免打扰立即影响排队和发送前复核。
- 全局暂停阻止所有自动外发、探索及自动日记归档/推送，用户明确请求及本地读取仍可用。外部已发出的请求无法撤回。
- 搜索词只发给配置的 SearXNG，不下载搜索结果 URL；结果标记为不可信资料，不执行其中指令。探索只用公开主题，不直接发布。
- 全局动作记忆为用户明确要求：只检索审核通过、long_term_allowed、有效且个人未关闭的条目，保留来源/可见范围。分页可访问所有合格条目，自动上下文有字符与条数上限。
- 模型记忆写入工具始终仅产生候选，不能自行批准。全局检索可以在记忆页关闭。传统会话自动检索继续隔离。
- 模型动作可参考跨会话记忆，但不能自行授权披露；QQ 发送、工具参数和空间正文增加跨会话原文检查。原文检查不等价于语义级防泄露，公共行为仍使用隐私提示、审核选项与服务端权限。
- 日记事实只来自平台回执，角色表达不是事实证据；周摘要按作用域和时区隔离。删除日记同步使对应周摘要失效，个人清理也清除周摘要。
- 给主人摘要只汇总主人自己的私聊和机器人空间计数，不汇总其他人的私聊或群内容，默认仅本地。任务进入独立待审批队列，不能把日记生成当作发送授权。
- 费用未知不显示零成本；当前预算只预警，超阈值及未知报价都不拦截自动或手动调用。已发出的失败/取消保守记账，恢复备份也不退回现有消耗。

## 第三方契约与实机验收

| 组件 | 已核实契约 / 历史真实验证 | 仍须按实际环境复验 |
| --- | --- | --- |
| NoneBot2 / OneBot V11 | 原生初始化、反向连接、get_status、文字发送回执 | 本轮私聊/群 @ 及媒体结果见发布报告，用户收到需独立确认 |
| NapCat QQ 空间 | 仅 v4.18.38 原生 send_qzone_msg，content/images/ugc_right，只有有效 tid 才确认成功 | 其他版本不伪报支持；好友/公开受众实发须遵守 72 小时/7 天限额 |
| meme-generator | 固定 0.1.14，真实安装 2869 素材、295 模板、JPEG/GIF、崩溃恢复；/meme/version 健康接口 | 独立服务端口、当前素材与 QQ 媒体端到端 |
| 额外模板 | contrib、meme_emoji、tudou 三仓库固定提交/哈希，历史组合 857 模板实际加载 | 上游更新需重新核验提交与许可，不能随意执行安装脚本 |
| HTML Render | 固定模板、Chromium、中文字体及 PNG 真实验证；禁脚本/网络/本地资源 | 当前机器浏览器、字体和 QQ 图片回执 |
| TTS / ASR | Edge 实际音色/MP3、本地 CPU Whisper 真转写；兼容接口有契约测试 | 云端语音服务需独立有效凭据；Edge 可能变更 |
| LLM / 生图 | DeepSeek 普通/流式及空间草稿有真实验证；生图有契约测试 | 云端生图需有效凭据，不能用假结果代替 |
| Search | 官方 SearXNG GET /search，q/format=json，结构与错误契约测试 | 用户配置可用实例后真实搜索；公共实例可能禁用 JSON |

官方依据：[NoneBot 接入](https://onebot.adapters.nonebot.dev/docs/guide/setup/)、[NapCat 安装](https://napneko.github.io/guide/napcat)、[NapCat 4.18.38 空间源码](https://github.com/NapNeko/NapCatQQ/blob/v4.18.38/packages/napcat-onebot/action/extends/SendQzoneMsg.ts)、[meme-generator](https://github.com/MemeCrafters/meme-generator)、[SearXNG Search API](https://docs.searxng.org/dev/search_api.html)。扩展哈希在 `scripts/meme-sources.json`，许可证随下载的第三方仓库保留；不将它们的代码、运行缓存或私人素材提交到 SoulBot。

## 开发验收与发布

核心服务保持模块化：NoneBot 只接收/发送，Agent 规则过滤与有界工具，Provider 按能力注册，Pydantic 统一 Schema，SQLite 持久预算/队列，原生 ES Modules 管理页。新功能不绕过执行器、授权或费用记录。

本地运行 README 的 Python/Node/静态检查；多模态真实检查可使用 `scripts/verify_meme.py`、`verify_extensions.py`、`verify_render.py`、`verify_tts.py`、`verify_asr.py`、`verify_image.py`，按脚本说明准备组件。这些脚本不能代替真人 QQ 来信或其他操作系统实测。

`scripts/ci.yml.example` 提供 Python 3.11–3.13 与 Linux/macOS/Windows 的 GitHub CI 模板，含 Node 页面生命周期回归、Ruff、发布版本/敏感路径检查。需有 workflow 权限的维护者将模板启用为 Actions 工作流；本次仅保留模板，未上线运行，不声明 CI 通过。版本使用 vX.Y.Z，包常量、WebUI、version.me 和 Git 标签一致；只在检查通过后提交/打标签/推送，并核对远程引用。禁止提交 .env、数据库、记忆、日记、日志、.venv、第三方缓存与临时截图。


## v2.0.1 本轮验证记录（2026-10-28）

- 本地回归：409 项 Python 测试、3 项 Node 页面生命周期测试通过，14 个原生 ES Module 语法检查、Ruff 和版本/敏感文件检查通过。
- Microsoft Edge 检查登录、模型工具验证确认弹窗、搜索、系统备份、人格、全局记忆及 ComfyUI 配置页面；桌面/手机截图完成，无页面 JavaScript 异常。

- 真实 DeepSeek `deepseek-flash` 已返回 `capability_probe` 原生函数调用，随后通过新接口验证并启用了当前实例的工具能力。
- 聊天审计确认原“直接画”请求进入 `generate_image`，本地 ComfyUI 0.42.0 / MPS / SD1.5 返回了有效 PNG，图片费用记录为 completed。
- 当次真实链路暴露了后续文字轮次的 task_budget 错误，已修复实际 Token 结算，并增加文字收尾失败仍交给执行器发送已生成媒体的回归测试。
- 此前本轮真实 meme-generator 图片生成及主人 QQ 图片发送已获得平台确认；修复后的 ComfyUI → QQ 完整实机复验尚未完成，最终复验时 6099 / 8188 未监听，不能报告成功收到。
- 云端生图、SearXNG 真实服务与其他操作系统实机仍需对应环境；GitHub CI 以远端运行结果为准。

模型说“不能画图”时，先检查工具能力开关、图片 Provider 启用/配置、工具权限，再查看审计中的 agent_tools_unavailable / tool_started / tool_completed / agent_caption_skipped / qq_sent。模型拿到附件不等于 QQ 平台已确认发送。工具参数固定后进入审批队列的请求只执行一次，审批本身不自动向 QQ 外发。

ComfyUI 仅支持本机回环 HTTP。生图页可选 checkpoint、尺寸、steps、CFG、sampler、scheduler 和负面提示词；不接受模型生成的任意工作流。默认每次一图，关闭服务不影响文本聊天。


## v2.0.2 开发与实测记录（2026-10-28 至 29）

本轮补齐当前会话 create_diary 与主人私聊 create_qzone_draft；两项动作不直接推送日记或发布空间。模型可按实际上下文容量使用 discover_tools 载入所需工具说明，最多3项，仍受5次工具/4轮模型和当前权限约束。

按用户要求移除预算拦截。所有 LLM、TTS、ASR、生图、搜索和主动任务均保留账本与预警，超过 Token/费用阈值和未知价格时继续调用。同日同类严重程度的预警去重记录。模型实际上下文和回复长度、限频、免打扰及权限仍执行。

真实 NapCat 4.18.38 在线；本轮 QQ 文字、meme 图片、Edge 语音均获得平台回执。实际读取322个音色，合成MP3并经QQ发送；本地Whisper完成3.912秒测试语音转写；Chromium生成800px中文PNG，上传Emoji后中文检索成功。三个扩展源独立生成JPEG/GIF，组合857个模板，禁用ID和持久限频检查通过。空间能力检测和DeepSeek公开主题草稿成功，72小时冷却返回409，未新增空间发布。

ComfyUI 0.42.0 的 SD1.5 使用中文提示词时实测出现主体偏差。依据模型卡说明（主要英文训练，非英文表现较差），增加可配置的模型工具提示词语言，默认指导LLM生成英文。实际DeepSeek调用generate_image，ComfyUI生成橘猫PNG，QQ获得发送回执；测试时把费用、当日Token、请求与任务阈值设低，仍成功调用并留下预警。模型卡：[SD1.5](https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5)。网页手动输入提示词不自动翻译。

修复Whisper缓存已准备但重启后仍提示首次安装的问题。全局记忆自动注入按实际模型上下文容量保留空间，完整合格条目仍可分页检索；预算输入阈值不会阻止调用。

仍需对应环境或真人输入的项目：SearXNG实例当前未提供可用服务；群@和真实QQ入站语音须用户实际发送，不能用伪造事件冒充；云端TTS/ASR/生图需要独立凭据。空间新公开动态实发须等待现有冷却，不能用本轮草稿测试宣称已经发布。

最终自动回归：433项Python测试、3项Node页面生命周期测试、14个ES Module语法及Ruff检查通过。实测临时调低的预警阈值已恢复原值（每日30万Token、5元，单次输入8000、单任务12000），全部仅用于预警。已清理本轮创建的Emoji夹具并验证删除重索引，撤销未修改的测试空间草稿，未新增空间发布。

最终真实WebUI检查：预算预警面板、已恢复的阈值、日记/空间草稿工具权限和ComfyUI英文选项均正常；390px手机宽度无横向溢出，无JavaScript页面异常。
