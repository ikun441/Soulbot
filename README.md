# SoulBot

**v2.0.3** · Python / NoneBot2 / FastAPI / SQLite / 原生 WebUI

可配置人格、模拟情绪、好感度、长期记忆、自主社交与多模态 QQ 机器人。粉色渐变毛玻璃界面支持图片、GIF 和视频壁纸。人格与情绪属于软件模拟，不代表真实意识或主观情感。

## 启动

准备 Python **3.11–3.13**（推荐 3.11）和可访问外部服务的网络，下载项目：

```bash
git clone https://github.com/ikun441/Soulbot.git
cd Soulbot
./start.sh
```

Linux/macOS 在“终端”粘贴以上命令；如提示无执行权限，运行 `bash start.sh`。Windows 可下载仓库 ZIP、解压，安装 Python 时勾选“添加到 PATH”，然后双击 `start.cmd`。

启动脚本自动创建项目内 `.venv`，安装依赖并初始化数据，不全局安装 Python 包。终端显示 WebUI 地址，默认 <http://127.0.0.1:2526>。终端须保持运行，按 **Ctrl+C** 停止。

首次进入按中文向导创建管理员、配置 QQ 和模型。选配能力可跳过，普通用户无需编辑 YAML。端口如已修改，以启动输出为准。

首次向导提供 DeepSeek 官方注册、充值与 Key 配置入口，以及 ComfyUI 官方安装指引和真实连接探测；Emoji 为内置功能，可选初始化并创建分类。仪表盘卡片提供相关快捷操作。

插件市场从 `ikun441/soulbot_plugins` 的版本化索引读取条目；仓库初始索引为空时不会显示虚构插件。下载后默认关闭，管理员明确启用才加载 Python 代码。插件为受信任代码，不提供操作系统沙箱；密钥单独保存，不通过普通读取接口返回。

## 连接 QQ 与模型

1. 按 [NapCat 官方指南](https://napneko.github.io/guide/napcat) 安装并启动 NapCat，扫码登录机器人 QQ；SoulBot 不替代 QQ 客户端。
2. SoulBot 的“QQ 连接”选择**反向 WebSocket**，设置主人 QQ，启用 QQ 并保存。
3. 在 NapCat 网络设置添加 **WebSocket 客户端**，填入 SoulBot 页面显示的接收地址，默认 `ws://127.0.0.1:2526/onebot/v11/ws`。
4. 同机且仅监听回环地址时双方 Token 可留空；远程访问或监听 `0.0.0.0` 时双方必须配置相同 Token。
5. QQ/监听/接收路径修改后，停止并重启 SoulBot。检查 QQ 状态，再用页面的测试发送或向机器人私聊、群聊 @ 测试。
6. 在“AI 模型”填入服务商的 Base URL、Key 和模型 ID，保存并运行真实连接测试。模型列表从服务商读取，费用价格未知时显示“未知”。

需要机器人画图或发表情时，在“AI 模型”点击“验证并启用工具调用”，确认模型返回真实函数调用后再开启能力。测试会计入预算。生图页选择本地 ComfyUI、填写回环地址和已安装的 checkpoint，读取实际模型后保存；AI 只填写画面描述，工作流和参数由管理员控制。超过两句的 QQ 文字默认每两句发送一段，可在聊天行为设置中调整。

连接方向：`QQ → NapCat → OneBot V11 → SoulBot`。NapCat WebUI 端口、NapCat 正向 WS 服务端口、SoulBot 接收端口是不同概念，请按页面说明填写。

## 能力与管理入口

| 功能 | 管理页面与行为 |
| --- | --- |
| 人格、情绪、好感度 | 模板/导入导出/试聊、可编辑的全局五维状态机、按会话隔离关系 |
| 长期记忆 | 候选审核、可信度/重要性/过期、个人关闭与删除；模型动作可检索所有获授权记忆作为内部参考，保留可见范围 |
| QQ 交流 | 被动/自然/活跃群聊、预过滤/聚合/冷却/免打扰，普通群消息不逐条调用模型 |
| 多模态 | Emoji、真实 meme-generator 及三个可选模板仓库、HTML 卡片、Edge/兼容 TTS、本地/兼容 ASR、云端 / 本地 ComfyUI 生图 |
| 搜索与探索 | 配置启用 JSON 的 SearXNG；公开主题探索默认关闭，只存本地记录 |
| 日记与周摘要 | 平台确认事实、当天预览、每周隔离汇总、独立模型润色及给主人待审批摘要 |
| 主动私聊 | 用户须私聊明确授权，管理员创建或启用 AI 生成草稿并审批任务，持久次数和冷却 |
| QQ 空间 | 草稿、可选审批、免审批自主发布和受众设置，至少间隔 72 小时、每 7 天最多 2 条 |
| 设置与动作记忆 | 默认在模型、工具和 QQ 发送前检索局部与获授权的全局记忆；可分别关闭；可选后台候选提取与情绪/好感度反馈 |
| 插件市场 | 文件夹插件、元数据与参数表单、下载校验、独立启停和工具权限；[开发规范](https://github.com/ikun441/soulbot_plugins/blob/main/PLUGIN_SPEC.md) |
| 系统与备份 | 暂停全部自主行为、下载私有 ZIP、上传校验并在下次启动恢复 |

搜索实例须启用 `search.formats: [html, json]`，在“搜索与探索”填地址。自建且无按次 API 费用时可显式填价格 `0`；价格未知时显示预警，继续调用。外部组件由用户按需启用；meme-generator、Chromium 和本地模型由项目隔离管理，不静默安装系统服务。

启用本地日记及模型工具能力后，可以在 QQ 中说“保存今天的日记”；模型调用 `create_diary`，只整理当前会话已确认的活动，事件不足会跳过。主人私聊可请求“把这段公开文字保存为空间草稿”，模型调用 `create_qzone_draft` 后可在 WebUI 查看；工具不直接发布，账号和受众沿用服务端配置。两项工具均可在“工具权限”关闭或设为审批。

Token 与费用预算只用于预警，不因超阈值或未知报价暂停调用；统计、估算和预警记录持续保留。模型实际上下文、回复长度、工具轮数、发送频率和用户授权分别管理。

记忆可以影响动作判断，不能覆盖权限、免打扰或发布边界。模型只能提出记忆候选，不能自行批准。空间自主发布只有明确开启时执行，未知接口版本保留草稿，不确定结果不重发。

## 用户命令

- `/记住 内容`、`/记忆 开启`、`/记忆 关闭`、`/忘记我`。
- `/主动联系 允许`、`/主动联系 禁止`、`/免打扰 开启`、`/免打扰 关闭`，仅私聊可授权主动联系。
- `/日记 开启`、`/日记 关闭`、`/删除日记`。
- 主人/群管理员：`/soulbot 状态`、`/soulbot 静音 15`、`/soulbot 恢复`。

群聊个人命令需 @机器人。删除和关闭立即持久化，授权不由模型伪造。

## 账号、备份与更新

普通用户使用“登录账号”修改用户名及密码。忘记密码时停止 SoulBot，在 `.env` 设置 `SOULBOT_WEBUI_USERNAME` 与 `SOULBOT_WEBUI_PASSWORD` 后重启；新密码须 12–128 位，启动后自动转换为哈希。反代地址用 `SOULBOT_ONEBOT_REVERSE_WS_URL`，完整说明见 [排查与运维](docs/debug.md)。

更新前在“系统与备份”下载私有 ZIP，再停止 SoulBot：

```bash
git pull --ff-only
./start.sh
```

Windows 更新代码后再次双击 `start.cmd`。不要删除 `.env`、`config.yaml`、`data/`、`emoji/`。恢复备份需先上传校验，再停止并启动；会自动保存旧数据、退出旧会话并暂停自主行为。备份含密钥和私人记录，请勿公开分享。

## 开发与验证

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
node --test tests/web/*.test.mjs
.venv/bin/python scripts/release_check.py
```

详细运行边界、依赖核查、真实服务验收和故障恢复集中在 [debug.md](docs/debug.md)；接口及配置说明见 [api.md](docs/api.md)；完整历史见 [version.me](docs/version.me)。跨平台 CI 模板见 [scripts/ci.yml.example](scripts/ci.yml.example)，启用需要仓库 workflow 权限；本次本地测试结果与远端 CI 状态分别报告。

## 社群
<img width="1284" height="2283" alt="微信图片_20261030190348_75_72" src="https://github.com/user-attachments/assets/c3b0b239-6807-4ba4-87a1-559a94e62504" />

