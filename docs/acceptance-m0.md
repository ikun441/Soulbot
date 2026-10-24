# M0 验收记录 · v0.1.0

日期：2026-10-24。环境：macOS arm64、Python 3.11.17、Microsoft Edge（Playwright 独立会话）。

## 已验证

- pytest：31 项通过，包含配置/持久化、秘密隔离、会话/退出/限流、来源检查、壁纸验证、Registry 能力隔离、工具权限/次数/超时、版本一致性和 OneBot 禁用网关。
- 上游 Starlette TestClient 有 1 条 httpx 弃用警告，无测试失败。
- Ruff、Bash 语法、两个 ES Module 语法检查通过；pip check 无依赖冲突。
- start.sh --check 首次检查和重复运行均通过，正式 start.sh 实际启动成功。
- NoneBot2 2.5.0、OneBot 适配器 2.4.6 与 FastAPI 0.162.0 真实共存启动。
- 管理员建号、八步向导、刷新恢复、中文人格保存、完成向导、健康检查、壁纸上传、动态开关、弹窗交互均经真实浏览器操作。
- 完全停止并重启隔离实例后，管理员会话、完成状态、中文人格、壁纸和动态设置保持。
- 原生页面过渡/侧栏指示/卡片与控件动效真实运行；系统减少动态时运行中的动画为 0；浏览器无新增脚本错误。
- 手机视口 390px，文档宽度 390px，无横向溢出；桌面 1440px 截图人工复核。
- Python wheel 版本为 0.1.0，包含 HTML/CSS/JS/SVG 静态文件（含新增 motion 模块）。

## 未验收与阶段边界

- Windows/Linux 的启动脚本与 Python 3.12/3.13 提供了 CI 待启用模板，尚未实际在本机验证；本次未启用 GitHub Actions。
- 本机用户给定的 2525 端口初始无监听，真实 get_status 检测失败会正确提示，不能宣称 QQ 已接通。
- M1 才实现 QQ/LLM 消息；meme-generator 安装/生命周期、TTS/ASR/图片/HTML Render 按 M2 实测，不包含在 M0 成功范围。
- M3–M5 的记忆/情绪、自主行为、日记、QZone 当前关闭。

验收数据在被忽略的 output/acceptance，未写入实际使用账户。实际 2526 实例保留首次管理员创建状态，用户指定主人 QQ 只保存在被忽略的本机配置。
