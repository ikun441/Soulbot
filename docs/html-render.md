# HTML 卡片 · v0.6.0

卡片工坊通过本机 Chromium 把文字、引用或清单渲染成 PNG。它是一项独立的 `html_render` Provider 能力，不能替代云端自由生图。当前只接收结构化文字字段，模板中的所有正文会转义；模型没有提交任意 HTML、CSS、脚本、路径或资源 URL 的接口。

## 使用步骤

1. 登录 WebUI，进入“卡片工坊”。
2. 点击“准备 / 检查 Chromium”，确认下载。浏览器和中文字体保存在 `data/html_render/`，首次可能需要几分钟和数百 MB 空间；可取消后重试。
3. 准备任务会实际启动浏览器，成功后才显示就绪。不会自动安装系统组件或系统服务。
4. 启用 HTML 卡片并保存。选择文字卡、引用卡或清单卡，以及樱花粉、浅紫或薄荷绿配色，填写内容后生成。
5. 成功后可预览或下载 PNG。发送给主人 QQ 需单独确认，且要求 NapCat 已连接、QQ 已启用并设置主人 QQ 号。

文字/引用卡需要正文；清单卡至少一项、最多 12 项，每项最多 120 字。标题最多 80 字，正文最多 1200 字，署名最多 100 字。超出图片高度会提示减少内容，不会截去正文。

## 配置

| 字段 | 默认值 | 范围 / 作用 |
| --- | --- | --- |
| html_render.enabled | false | 启用/关闭生成，安装准备可在关闭时进行 |
| html_render.width | 800 | 480—1200 像素 |
| html_render.max_height | 1800 | 400—2400 像素；按内容自动确定实际高度 |
| html_render.timeout_seconds | 20 | 5—60 秒，含排队时间 |
| tools.render_html | auto | AI 工具权限：deny / auto / approval；当前 approval 拒绝执行 |

最多同时渲染 2 张卡片，总待处理任务最多 4 个，达到上限时拒绝额外请求。网页手动操作需要管理员认证；工具权限控制 AI 调用。QQ 工具产物按会话保存，跨会话不能读取。管理预览需要管理员登录。图片保留 24 小时，与现有媒体清理规则一致。

## 安装和边界

- 固定 Playwright 1.64.1，使用官方 CLI 下载匹配的 Chromium。每次准备均执行实际启动检查。`PLAYWRIGHT_BROWSERS_PATH` 只传给子进程，不修改系统 HOME。[官方浏览器安装说明](https://playwright.dev/python/docs/browsers)。
- 中文字体沿用固定 meme-generator v0.1.14 的 Noto Sans SC 资源；已存在的相同字体可复用，但必须匹配 SHA-256 `ae82f4e2a55e1316a55bcc1d05e9555ce08d8bda07e893b486896b626fd852ff`。[上游字体来源](https://github.com/MemeCrafters/meme-generator/tree/v0.1.14/resources/fonts)。
- 每个生成任务拥有独立子进程和全新浏览器上下文；网页脚本关闭、Service Worker 禁止、网络离线且路由拦截、CSP 限制资源，只有固定且经过校验的字体以 data URI 嵌入。不读取任意本地文件。
- 渲染子进程不继承机器人的 API Key、WebUI 密码或 OneBot Token。正文通过标准输入传入，不作为命令参数，也不写入日志。审计只记录工具/模板类型。
- 超时、取消和停止 SoulBot 会清理自己创建的子进程。安装失败不阻塞文本或其它媒体功能。重启保留浏览器与字体，重新检查可用文件。

## 常见问题与验证

准备下载失败：检查网络与磁盘，再点击准备。安装不会自动运行 `sudo` 或系统软件包管理器。

Linux 提示浏览器无法启动：检查 [Playwright 系统要求](https://playwright.dev/python/docs/intro#system-requirements) 与缺失共享库/沙箱权限，建议使用普通用户运行。系统库由维护者按发行版安装，SoulBot 不会静默安装。Windows/Linux 尚待对应实机验证，本版真实验收为 macOS arm64 / Python 3.11。

开发者可执行 `PYTHONPATH=. .venv/bin/python scripts/verify_render.py --install`，下载到项目目录并在 `output/render-live` 生成真实验收图。后续运行去掉 `--install`。脚本验证三种卡片、注入文字、超高拒绝、禁用拒绝、认证 API、真实工具出图和会话隔离，不改变正式 QQ/模型设置，也不会向 QQ 发送消息。
