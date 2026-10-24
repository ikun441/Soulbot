# 架构 · v0.1.0

入口 main.py → 构建配置/SQLite/密钥/Registry 服务 → NoneBot2 FastAPI + HTTPX + WebSocket 驱动 → OneBot V11 适配器 → 挂载管理 WebUI。ASGI 外层网关在 QQ 关闭时拒绝 OneBot 请求。

管理应用不处理消息，路由只调用共享服务。M1 在 adapters 中将消息转换为 core 标准事件，Agent 经规则过滤、人格/记忆、决策、Provider 与 Tool，再由权限引擎确认外部动作。M0 无消息监听器，因此不会误回复。

## 当前模块

- services/config_service：Pydantic Schema、原子 YAML、上一版备份与恢复。
- services/secret_service：`.env`/环境变量与 0600 JSON 密钥，API 仅提供配置存在标记。
- services/auth_service：加盐 PBKDF2、随机会话、会话摘要入库、失败登录限流。
- storage/database：SQLite 事务、schema_version、管理员、会话、向导、审计。
- providers/registry：能力维度注册/解析，跨能力不能替代。
- tools/registry：Pydantic 参数、默认拒绝权限、服务端调用上下文、超时与任务总次数限制、审计。
- web/static：原生 HTML/CSS/JS；后台状态与表单均使用真实 API。

## 状态与生命周期

运行根目录默认项目根，可用 SOULBOT_HOME 隔离。配置与数据库建立即幂等，不覆盖原文件。端口/QQ/Token 应用依赖重启；人格、预算预设、壁纸等管理配置保存后立即读取。当前尚无 AI/预算执行器，不能因保存了上限就声称调用已受限。

当前未注册真实 Provider 或 Tool；框架已测试，具体实现按阶段接入。NoneBot 全局初始化只在真实入口执行，测试管理 API 使用独立应用。
