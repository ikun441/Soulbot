# M1 验收记录 · v0.2.0

日期：2026-10-25。此记录区分了代码测试与外部服务实测。

## 已验证

- `PYTHONPATH=. TMPDIR=output .venv/bin/pytest -q`：47 项通过。
- Ruff lint/格式检查、JavaScript `node --check`、`git diff --check` 通过。
- Provider 使用 HTTPX MockTransport 验证 Chat Completions 请求、真实模型列表解析、SSE 增量与用量、认证/404/限流/服务端错误分类、连接中断和备用模型切换。它是协议夹具，不是模型服务商。
- SQLite 测试验证 schema v2、保留旧向导配置、用量持久化、未知价格标记与并发 Token 预留不会超支。
- Agent 测试验证普通群消息不调用模型、明确触发消息聚合、冷却/静音、私聊与群聊上下文隔离及重复事件抑制。
- API 测试验证登录保护、真实 Provider 接口接线、用量读取、网页试聊，以及发送测试消息只允许显式确认并固定发给主人。
- 启动隔离验收实例（`output/acceptance`，本机 `127.0.0.1:2527`），Edge 登录后实测仪表盘、模型页、群聊权限编辑和用量页；保存群策略后列表显示成功。隔离实例已停止。
- `main.py` 正常启动并显示 v0.2.0/M1，WebUI 返回可用。

## 尚待外部联调

- 需要已登录的 NapCat / QQ 和有效 OneBot Token，验证真实反向 WebSocket 握手、`get_status`、私聊/群聊事件与 QQ 消息回执。
- 需要用户配置的真实 LLM API Key 与模型，验证真实服务商模型列表、聊天、SSE 兼容性和账单价格。
- 当前无外部凭据时，无法据实声称 NapCat 或模型商端到端成功。测试夹具没有替代真实 API，也没有发送实际 QQ 测试消息。
- 当前环境只验证 macOS / Python 3.11；Linux 和 Windows 尚未实机验收。

## 测试注意

依赖发出一条 Starlette `TestClient` 关于 httpx 的弃用警告，不影响 47 项通过。CI Actions 尚未启用；现有推送凭据不含 GitHub Actions workflow 权限。
