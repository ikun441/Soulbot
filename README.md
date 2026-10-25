# SoulBot

**当前版本 v0.2.0 · M1 QQ 与基础 AI**

具备可配置人格、模拟情绪、长期记忆与明确行为边界的 AI QQ 机器人平台。当前交付基础服务、QQ 文本消息处理、OpenAI 兼容 LLM 与原生 WebUI。人格和情绪均为软件模拟，不代表真实意识或主观情感。

## 开始使用

先安装 Python 3.11–3.13，然后在项目目录运行：

```bash
./start.sh
```

Windows 双击 `start.cmd`。打开 <http://127.0.0.1:2526>，按向导创建管理员账户。首次启动自动创建 `.venv` 并安装依赖，不需要编辑 YAML。

详细步骤见 [start.md](start.md)。完整路线见 [实施计划](IMPLEMENTATION_PLAN.md) 和 [任务清单](TASKS.md)。

## 目前可用

- Python / NoneBot2 FastAPI 驱动 / OneBot V11 适配器初始化。
- SQLite 管理员、持久会话、向导进度与审计。
- 统一 Pydantic 配置、原子保存、上一版备份恢复、脱敏导出。
- 独立密钥文件和 `.env` 支持；WebUI 不回显 Key。
- 八步中文向导，默认推荐 NapCat；OneBot V11 文本消息收发和向主人发送测试消息。
- 粉色渐变毛玻璃、响应式布局、本地图片/GIF/视频壁纸、动态开关。
- Provider Registry、Tool Registry、默认拒绝权限和限次/超时执行基础。
- OpenAI 兼容 Chat Completions、模型列表读取、有限重试/备用服务和可选 SSE。
- QQ 私聊、群聊 @/唤醒/连续对话过滤、聚合、持久冷却、全局发送限速与管理员静音命令。
- Token/费用预留、用量记录、网页试聊和群/私聊短期上下文隔离。

## 开发边界

NapCat、模型服务和密钥需用户自行配置。当前环境没有可用的 NapCat 登录会话或 LLM API Key，因此代码与本地协议夹具的验证不等于外部端到端验收。TTS/ASR/图片/meme-generator 在 M2 联调；持续人格/记忆在 M3，自主行为/日记在 M4，QQ 空间在 M5。所有未验收能力在界面中明确标注。

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

[版本记录](docs/version.md) · [架构](docs/architecture.md) · [配置](docs/configuration.md) · [第三方核查](docs/providers.md)
