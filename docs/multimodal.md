# 多模态工坊 · v0.9.0

本页介绍 Emoji 和 meme-generator。HTML 卡片独立提供固定排版能力，见 [卡片工坊说明](html-render.md)；语音见 [语音工坊](tts.md)，云端自由生图接口见 [云端生图](image-generation.md)，真实服务尚待凭据验证。

在 WebUI 打开“Emoji 表情库”，可以直接上传 PNG、JPEG、GIF、WebP，或把文件放进项目 emoji 目录后点击“重新索引”。子目录即分类，支持中文文件名。每次搜索候选数和分页数量只控制一次返回数量，不限制表情库容量。标签与别名保存在 SQLite，重启后保留。

文件访问使用库内 ID。路径、符号链接、任意网址均不能由模型直接读取。图片最多 16MB、1600 万像素、300 帧，总帧像素不超过 1 亿。上传重复名称会另存，已有文件不会覆盖。

## 安装 meme-generator

在“表情包工坊”保持“由 SoulBot 托管服务”，默认地址是 `http://127.0.0.1:2233`，与 SoulBot 的 2526 管理端口分开。点击“安装 / 修复素材”，或启用服务并保留“首次启用时自动安装”。

安装器只在项目 `third_party/meme-generator/.venv` 安装固定版本 0.1.14。该上游要求 Pillow 10，与主环境的 Pillow 12 隔离。资源来自官方固定 v0.1.14 地址，逐个核对官方清单的 MD5，并准备 Noto Sans SC 字体；没有更改系统 HOME、全局字体或系统服务。初次下载会花几分钟，进度状态在页面显示。失败后可修复，已校验文件会复用。

运行文件包括独立环境、config/cache/data/fonts、ready.json、service.lock、state.json 和 service.log，全部位于忽略的 third_party 目录。安装日志最多保留 1MB。单实例锁在进程退出或崩溃时由操作系统释放，不会仅凭 PID 接管陌生进程。

启用后，SoulBot 在后台启动服务并读取真实 `/meme/version` 和模板列表。异常时按配置有限重启；达上限会停止尝试并显示错误，核心 WebUI/文本聊天继续可用。停止 SoulBot 会停止自己创建的子进程。手动“停止本实例服务”保留文件；下次启动 SoulBot 时，若配置仍启用，将再次启动。

如果已有服务，关闭“由 SoulBot 托管服务”，填写外部 Base URL。外部模式只调用 API，不执行安装或停止。当前只验证 0.1.14 契约，其它版本会明确拒绝健康检查。

## 制作和发送

点击“测试真实 API”读取模板，按英文模板 ID 搜索，例如 `raise_sign`、`petpet`。选择模板后显示实际图片/文字数量、默认文字及参数 Schema。文字每行一段，图片可本地上传，最多 6 张。参数不符会拒绝请求；生成请求不会自动重试。成功后显示真实图片、格式、尺寸与大小，可下载或确认后发送给配置中的主人 QQ。

测试发送需要 NapCat 已登录并连接、QQ 接入权限开启，以及人格配置中的主人 QQ 号。每分钟最多发送一张测试图片。平台没有有效发送回执时不会报告成功。图片产物 24 小时后不可读取，下次生成或启动清理；本地 Emoji 不按此期限删除。

AI 生成图片绑定机器人、群/私聊及用户的会话范围，跨会话即使知道 ID 也会拒绝读取。管理端上传图片由登录管理员管理，不会自动提供给 QQ 会话；本地 Emoji 是显式共享的表情库。

## AI 媒体工具

在 AI 模型高级设置中，仅当模型确实支持 Tool Calling 才开启声明。工具权限页可设为自动、禁止或需要审批。审批队列尚未交付，设置“需要审批”会拒绝调用。

已接入 search_emoji、browse_emoji、select_emoji、list_meme_templates、get_meme_info、generate_meme。模型只传图片 ID 和结构化参数；不能执行 Shell、任意文件、网址或更改配置。每轮 QQ 任务最多 5 次工具调用、4 次模型请求、最终 2 张图片；任务 Token 预算包含全部轮次、重试、故障切换和工具 Schema。预算不足会停止，复杂任务可在管理页调整上限。

网页试聊仍只请求文本。HTML 卡片和 TTS 已开放；ASR 见 [语音识别](asr.md)；云端自由生图接口已实现但未真实联调；QQ 来图继续开发。真实 QQ 图片端到端验收和真实模型 Tool Calling 需要用户提供可用服务，本地夹具测试不代表这两项已经联调。

## 真实验收与许可证

本次在 macOS arm64 / Python 3.11 真实安装并验证了 2,869 个资源、295 个模板、举牌 JPEG、petpet GIF、模板预览、参数拒绝、重复实例拒绝、子进程崩溃恢复和停止。可以运行 `PYTHONPATH=. .venv/bin/python scripts/verify_meme.py` 复验，使用本机 2234 端口和 output/m2-live 隔离数据。

上游代码以 [meme-generator MIT 许可证](https://github.com/MemeCrafters/meme-generator/blob/v0.1.14/LICENSE) 分发，安装在隔离环境中，未将源码、素材或字体提交到本仓库。上游说明模板素材来自网络；素材权利与代码许可证分别适用，详见[官方说明](https://github.com/MemeCrafters/meme-generator)。中文字体从其固定版本资源目录下载，仅用于本地渲染。本次未验证 Linux/Windows 原生组件安装。
