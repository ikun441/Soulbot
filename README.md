# SoulBot

**当前版本 v0.9.0 · 云端生图接口**

具备可配置人格、模拟情绪、长期记忆与明确行为边界的 AI QQ 机器人平台。当前交付基础服务、QQ 文本消息处理、OpenAI 兼容 LLM 与原生 WebUI。人格和情绪均为软件模拟，不代表真实意识或主观情感。

## 开始使用

先安装 Python 3.11–3.13，然后在项目目录运行：

```bash
./start.sh
```

Windows 双击 `start.cmd`。打开 <http://127.0.0.1:2526>，按向导创建管理员账户。首次启动自动创建 `.venv` 并安装依赖，不需要编辑 YAML。

详细步骤见 [start.md](start.md)。完整路线见 [实施计划](IMPLEMENTATION_PLAN.md) 和 [任务清单](TASKS.md)。

需要通过文件预设登录账号、恢复密码或配置反代 WS 时，参见 [.env 配置说明](docs/environment.md)。

## 目前可用

- Python / NoneBot2 FastAPI 驱动 / OneBot V11 适配器初始化。
- SQLite 管理员、持久会话、向导进度与审计。
- 统一 Pydantic 配置、原子保存、上一版备份恢复、脱敏导出。
- 独立密钥文件和 `.env` 支持；WebUI 不回显 Key。
- `.env` 保存/应用 WebUI 管理账号与密码哈希；支持启动时预设或恢复账号，WebUI 修改账号会退出其他会话。
- 反向/反代 WS 地址可在 QQ 页面保存到 `.env`，支持自定义接收路径；填写地址后仍需独立启用 QQ 并配置 Token。
- 八步中文向导，默认推荐 NapCat；OneBot V11 文本消息收发和向主人发送测试消息。
- 粉色渐变毛玻璃、响应式布局、本地图片/GIF/视频壁纸、动态开关。
- Provider Registry、Tool Registry、默认拒绝权限和限次/超时执行基础。
- OpenAI 兼容 Chat Completions、模型列表读取、有限重试/备用服务和可选 SSE。
- QQ 私聊、群聊 @/唤醒/连续对话过滤、聚合、持久冷却、全局发送限速与管理员静音命令。
- Token/费用预留、用量记录、网页试聊和群/私聊短期上下文隔离。
- 本地 Emoji 中文索引/检索、上传、标签/别名、分页与 QQ 图片发送。
- 独立 .venv 托管真实 meme-generator：安装素材、状态检测、有限重启、模板参数/预览/生成。
- WebUI 可按需下载三个经过固定提交和 SHA-256 校验的额外表情仓库；各仓库默认关闭，可独立禁用模板 ID 和限制每分钟生成数。
- 扩展模板在专用 meme-generator 进程中加载；显示加载冲突/错误，主服务不执行仓库安装脚本。
- 表情库、表情包工坊、工具权限页，支持工具能力模型通过有界调用选择/制作图片。
- 卡片工坊：项目内 Chromium、校验中文字体、文字/引用/清单三种卡片、三种配色、私有 PNG 预览/下载和受限 render_html 工具。

## 开发边界

NapCat、模型服务和密钥需用户自行配置。三个额外仓库均已实际下载并加载，共验证 857 个模板及真实生成；HTML 卡片也已用真实 Chromium 完成中文出图和接口/工具验证。NapCat/QQ 与真实外部 LLM 联调仍需有效账户。TTS 已接入真实 Edge 服务与独立的兼容语音接口，支持试听、持久限额和会话隔离；ASR 已支持真实本地 Whisper 和独立云端兼容接口，提供上传转写与受限 QQ 语音处理；云端生图接口、权限与预算已实现，真实服务尚待独立凭据验证，M2 尚未整体验收；持续人格/记忆在 M3，自主行为/日记在 M4，QQ 空间在 M5。详见 [多模态说明](docs/multimodal.md)、[卡片说明](docs/html-render.md)、[语音说明](docs/tts.md)、[识别说明](docs/asr.md)、[云端生图说明](docs/image-generation.md) 和 [进度与下一版计划](docs/progress.md)。

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

[版本记录](docs/version.md) · [架构](docs/architecture.md) · [配置](docs/configuration.md) · [第三方核查](docs/providers.md)
