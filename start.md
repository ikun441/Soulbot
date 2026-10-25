# SoulBot 小白启动指南

当前版本：v0.2.0（M1）。配置 NapCat 和 LLM 后可以使用 QQ 文本对话；语音、图片和长期记忆将在后续阶段接入。

## 1. 准备什么

- 推荐 16GB 内存的电脑或 Linux 服务器，不要求 2GB 内存适配。
- Python 3.11、3.12 或 3.13。当前实际验收为 macOS / Python 3.11；Linux 与 Windows 脚本尚需在对应系统实测。
- 能访问 Python 依赖下载源的网络。
- 后续阶段接入 QQ 需要独立安装 NapCat，AI 需要你自己的 API Key。

Python 安装入口：<https://www.python.org/downloads/>。Windows 安装时勾选 **Add python.exe to PATH**。macOS 自带的旧 Python 通常不能用，需要自行安装新版本。

## 2. 下载项目

打开 <https://github.com/ikun441/Soulbot>，点绿色 Code → Download ZIP，然后解压。也可在终端输入：

```bash
git clone https://github.com/ikun441/Soulbot.git
cd Soulbot
```

“终端”是输入命令的程序：macOS 在启动台搜索“终端”；Windows 在文件夹空白处右键，选择“在终端中打开”。后续命令都在解压后的项目文件夹运行。

## 3. 一键启动

**Windows：** 双击 `start.cmd`，保留打开的窗口。如果窗口提示找不到 Python，请先完成第 1 步。

**Linux / macOS：** 在项目目录的终端执行：

```bash
bash start.sh
```

首次运行自动创建 `.venv` 文件夹并把 Python 包安装在里面，不会全局安装。之后运行会检查依赖是否变化，已有配置和数据不会被覆盖。

如果有多个 Python，可指定解释器，例如：

```bash
SOULBOT_PYTHON=/你安装的Python路径/python3.11 bash start.sh
```

只检查依赖、配置与数据库，不启动服务：`bash start.sh --check`。

## 4. 打开 WebUI

等终端显示地址后，用浏览器打开：

**http://127.0.0.1:2526**

这是你自己的本机地址。保留终端窗口，关闭窗口会停止服务。默认不向局域网或公网开放。

第一次会进入八步向导：欢迎 → 管理员 → QQ → AI → 人格 → 多模态 → 权限 → 检查。管理员密码至少 12 位，建议使用密码管理器。点击下一步会安全保存配置；重新打开页面后登录即可继续。选配项目可以留空。

不要把 API Key 填进自我介绍或人格 Prompt，使用专门的密码输入框。前端不使用浏览器持久存储保存密钥。

## 5. NapCat 安装与 QQ 接入

NapCat 是登录 QQ 并提供 OneBot 协议的独立程序；SoulBot 是接收事件并管理角色的程序，两者需要分别运行。

1. 打开 [NapCat 官方安装入口](https://napneko.github.io/guide/napcat)。
2. 在文档的“安装方式”选择适合系统的方式（Shell 或 Framework），按当前官方步骤安装 QQ 依赖和 NapCat。不要把其它人发来的未知安装脚本直接运行。
3. 按官方说明启动 NapCat，打开它自己的 WebUI，扫码登录你的机器人 QQ。NapCat 的 WebUI 登录信息与 SoulBot 管理员无关。
4. 在 SoulBot 的“QQ 连接”保留“反向 WebSocket”。自己设置一个随机 Access Token，在 SoulBot 中保存。
5. 在 NapCat 网络配置中添加 **WebSocket 客户端 / 反向 WebSocket**，填写：`ws://127.0.0.1:2526/onebot/v11/ws`，并填入同一 Token，保存、启用。
6. 在 SoulBot 启用 QQ 适配器，保存后按 Ctrl+C 停止，再运行启动脚本。两个程序在同一台机器时使用 127.0.0.1。
7. 重启 SoulBot 后查看仪表盘 QQ 状态；在 QQ 设置页可以确认连接，并在确认提示后向主人 QQ 发送一条测试消息。测试发送每分钟最多一次。

连接关系：`QQ → NapCat → OneBot V11 → SoulBot`。

“本机测试 / 正向连接地址”是 NapCat WebSocket 服务端的地址，用于读取 `get_status`，与反向接收地址不是同一个概念。默认示例 2525 可能没有服务；检测失败时不能宣称 NapCat 已连接。

若 NapCat 在另一台机器：127.0.0.1 指的是各自所在机器，需要改为 SoulBot IP，并配置监听、防火墙和访问控制。这属于高级部署，见 [部署说明](docs/deployment.md)。

## 6. 配置 AI

在“AI 模型”中填入服务商提供的 Base URL、模型 ID 和 API Key。可以先保存并读取该账户真实可用的模型，再运行连接测试。连接测试和网页试聊会实际调用模型并计入用量；价格未知会明确显示为未知。确认群聊行为、Token 与费用限制后，@ 机器人或使用配置的唤醒词即可测试 QQ 回复。

多模态、meme-generator 和搜索都可跳过，当前版本不会静默下载或启动这些服务。

## 7. 自定义外观

“外观与壁纸”支持 PNG、JPG、GIF、WebP、MP4、WebM，最大 32MB。视频静音循环，关闭动态效果会暂停视频并隐藏 GIF 回退为渐变。系统启用“减少动态效果”时也会减少动画。壁纸随本机配置保留，不上传到外部服务。

## 8. 常见错误

| 提示 | 处理方式 |
| --- | --- |
| 找不到 Python / 版本不符 | 安装 Python 3.11–3.13，重新打开终端，必要时指定 SOULBOT_PYTHON |
| 依赖下载失败 | 检查网络，保留原文件后重试启动；不要用 sudo 全局安装 |
| 地址打不开 | 确认终端仍在运行；地址为 http 而非 https，默认端口 2526 |
| 端口占用 | 先检查是否已有 SoulBot 运行；避免重复启动。若需改端口，停止服务后修改 config.yaml 的 web.port |
| 配置校验失败 | 根据最近更改检查 config.yaml；保留文件，用 data/config.backup.yaml 恢复上一版 |
| QQ 未连接 | 检查 NapCat 是否登录、反向客户端是否启用、双方 Token 是否相同，并重启 SoulBot |
| QQ 无回复 | 检查 NapCat 登录与反向 WebSocket 地址/Token；确认 QQ 已启用并重启 SoulBot，群聊默认仅响应 @、唤醒词或有效连续对话 |
| 模型调用失败 | 在模型页面运行连接测试，核对 Base URL、模型列表、密钥、余额与上下文上限；查看安全操作记录中的错误类型 |
| 费用或 Token 用尽 | 用量页面展示已用与预留；重启不会清空每日额度，下一时区日自动开始新的统计 |
| 2525 检测失败 | 可能没有 NapCat 正向服务，或填了反向接收端；反向模式通过 NapCat 连接 SoulBot 的 2526 端口 |
| 登录失败过多 | 每个客户端 15 分钟最多 10 次失败尝试，等待窗口结束 |
| 视频不播放 | 可能格式编码不被浏览器支持，或动态效果关闭；请使用常见 H.264 MP4/WebM |

## 9. 查看记录和停止

“操作记录”查看最近的配置操作（时间标为 UTC），不记录聊天正文或 API Key。

在运行窗口按 **Ctrl+C** 停止程序，等待进程退出。请勿在运行中复制数据库文件作为备份。

## 10. 更新

1. 先停止 SoulBot。
2. 按下节备份。
3. 用 Git 下载的用户运行 `git pull --ff-only`；ZIP 用户把新版解压到新目录后复制原配置和 data。
4. 阅读 `docs/version.md` 的升级说明，重新启动。脚本会在 `.venv` 中更新依赖。

不要删除 `.venv` 以外的运行目录来“修复”问题；这些文件可能包含配置和记忆。

## 11. 备份与恢复

停止程序后，复制以下项目到安全位置：`config.yaml`、`data/`、实际存在的 `.env`、`emoji/`。`data/` 包含管理员哈希、私密密钥和数据库，不要放到公共网盘或 GitHub。`.venv` 无需备份。

WebUI 的“导出脱敏配置”只导出非敏感设置，不含密钥与主人 QQ。用户自行写入的文本仍需检查。“恢复上一版”只恢复配置，不恢复数据或密钥。

完整恢复：停止服务，把备份文件放回原位置，再启动。

## 12. 忘记管理员密码

M0 未提供自助重置入口。请保留原 data 备份，联系本机维护者离线重置管理员与会话；不要把整个数据库发到公开渠道。不要删除数据库，这会丢失状态。
