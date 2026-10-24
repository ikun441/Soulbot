# 开发约定

开发基线为用户提供的 SoulBot 需求说明书。后续按 IMPLEMENTATION_PLAN.md 和 TASKS.md 分阶段交付，不能把占位 UI 当成功能。当前阶段 M0。

- Python 3.11–3.13；使用 `.venv`。原生 ES Modules，不引入前端构建链。
- 业务服务独立于 QQ/管理路由，外部操作必须经权限/Tool 路径。模型参数不能构造审批或获得 Shell/文件/配置访问能力。
- 配置变更统一通过 Settings 验证；扩展字段需要同步 UI、文档和测试，未知字段直接拒绝。
- 数据库、密钥、日志、聊天、主人身份和本机测试配置不提交。
- 第三方集成需要固定版本、官方证据、真实请求与故障测试；无凭据不能标为验收成功。
- 版本常量 `soulbot.__version__` 遵循 PEP 440，展示 VERSION 和 Tag 使用 vX.Y.Z；包元数据动态读取同一常量。

验证：`.venv/bin/python -m pytest -q`、`.venv/bin/ruff check .`、`bash -n start.sh`、`node --check soulbot/web/static/js/app.js`，以及实际启动/浏览器检查。

发布：通过测试 → 更新 docs/version.md/检查版本一致性 → scripts/release_check.py → Commit → annotated Tag → push main/Tag → git ls-remote 确认。不要强推，不覆盖远程历史。认证失败保留本地提交并说明。

CI 模板位于 `docs/ci.yml.example`。本次推送 Token 缺少 workflow 权限，因此未上传活动工作流。取得对应权限后，可将模板复制为 `.github/workflows/ci.yml`，再推送启用。

管理接口写操作需要 `X-Soulbot-Request: 1`，浏览器同源；Cookie HttpOnly/SameSite Strict。开发时请使用临时 SOULBOT_HOME，避免测试管理员进入用户数据。
