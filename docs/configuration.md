# 配置说明 · v0.1.0

普通用户通过 WebUI 配置，不需要手改 YAML。首次启动自动生成 config.yaml，实际字段以 `Settings.model_json_schema()` 为准，登录后可访问 `/api/config/schema`。需求文档中的 YAML 是未来结构参考，不能直接把未实现字段复制到 M0。

| 配置区 | 当前字段 |
| --- | --- |
| soulbot | name、language、timezone |
| web | host、port（重启生效） |
| qq | enabled、connection、test_url（重启生效） |
| llm | base_url、model（M0仅保存） |
| personality | description、style、interests、owner_qq、owner_title、prompt |
| behavior | group_mode、quiet_hours、daily_tokens、daily_cost_cny；autonomous/diary/qzone 当前必须 false |
| appearance | motion、wallpaper、wallpaper_kind |

未知字段、无效端口/时区/非有限费用、非受支持的壁纸路径均被拒绝。失败不会覆盖现有文件。每次保存自动备份上一版至 data/config.backup.yaml；恢复操作会交换当前与备份版本。

密钥来源优先级：进程环境变量 → 项目 .env → data/secrets.json。变量为 `SOULBOT_LLM_API_KEY`、`SOULBOT_ONEBOT_ACCESS_TOKEN`。WebUI 输入写入 data/secrets.json（0600）；设置了环境变量时请在环境来源修改，WebUI 保存不会覆盖上游环境。

QQ 必须有 Token 才能启用。Token 修改需要重启。密钥不会出现在 YAML、GET 配置或导出中。导出还会清空主人 QQ，但不自动审查用户填写的 Prompt，请自行检查。

备份/数据库均为私密文件。完整备份应停止程序并复制 config.yaml、data 和实际 .env，详见 start.md。
