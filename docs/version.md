# SoulBot 版本更新日志

## v0.1.0 - 2026-10-24

### 新增
- M0 模块化项目、实施计划与分阶段验收清单。
- `.venv` 自动管理、start.sh、start.cmd 与简体中文 start.md。
- FastAPI、NoneBot2 驱动与 OneBot V11 初始化，默认关闭 QQ 接入。
- Pydantic 统一配置、SQLite、管理员哈希密码/会话、向导进度与操作记录。
- 配置原子保存、上一版恢复、密钥独立存储和脱敏导出。
- 八步首次配置向导与本机 OneBot 只读协议探测。
- 粉色渐变毛玻璃原生 WebUI、响应式布局、自定义图片/GIF/视频壁纸与动态开关。
- Provider Registry、Tool Registry 和默认拒绝权限基础。

### 优化
- 增加页面方向过渡、卡片错峰入场、滚动显现、侧栏滑动指示、按钮涟漪/加载反馈、开关回弹、通知/弹窗和壁纸渐变。
- 动态关闭与系统减少动态效果会停止界面动画；快速切换取消旧过渡，保持表单可操作。
- 普通配置无需手工修改 YAML；错误不会回显敏感输入。
- 未实现能力明确标示所属阶段，不展示伪造的服务成功结果。

### 兼容性变化
- 初始版本，无历史配置迁移。运行要求 Python 3.11–3.13；包版本按 PEP 440 为 0.1.0，UI 和 Git Tag 为 v0.1.0。

### 已知问题
- 尚不支持 AI/QQ 聊天；M1 开放真实 Provider 与消息处理。
- TTS/ASR、生图、meme-generator、HTML Render、记忆/情绪、日记、自主行为和 QZone 仍按 M2–M5 计划实现。
- NapCat/LLM 无实际可用登录/凭据时不能做端到端验收；协议检测失败会据实提示。
- Windows/Linux 启动需对应系统实测；当前验证环境为 macOS / Python 3.11。
- 当前 Token 缺少 workflow 权限，GitHub Actions 未启用；CI 配置保留为 docs/ci.yml.example。
- 视频兼容性取决于浏览器编码支持；M0 无完整管理员密码重置流程。

### 升级说明
- 首次运行自动生成 config.yaml 与 data，请勿提交这些文件。
- 更新前停止服务并备份 config.yaml、data/、.env 和 emoji/。
