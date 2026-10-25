# .env 账号和反向 WS 配置

从 v0.5.0 起，项目目录的 `.env` 可以设置 WebUI 管理账号和 NapCat 反向接收地址。普通用户可以使用“登录账号”和“QQ 连接”页面，保存结果会自动写入 `.env`。这些配置无需添加 Python 依赖。

## 通过文件设置

尚无 `.env` 时，可复制 `.env.example` 并重命名为 `.env`；已有文件请直接编辑，保留原有密钥。关闭 SoulBot 后填写：

```dotenv
SOULBOT_WEBUI_USERNAME=my_admin
SOULBOT_WEBUI_PASSWORD='请替换为至少12位的独立密码'
SOULBOT_ONEBOT_REVERSE_WS_URL=ws://127.0.0.1:2526/onebot/v11/ws
SOULBOT_ONEBOT_ACCESS_TOKEN='请替换为自己设置的随机Token'
```

重新运行启动脚本即可应用。用户名为 3—40 位字母、数字、下划线或短横线；密码为 12—128 位。用户名和密码必须成对提供。引号内的 `${...}` 按字面读取，不执行环境变量插值。

启动时会用独立随机盐生成 PBKDF2 哈希，把密码保存为 `SOULBOT_WEBUI_PASSWORD_HASH`，删除 `.env` 中的 `SOULBOT_WEBUI_PASSWORD` 行。SQLite 只保留用于登录校验的哈希副本，接口、日志和脱敏导出不返回密码或哈希。`.env` 使用限制访问的文件权限（POSIX 下为 0600），且被 Git 忽略。

## 创建、修改和恢复账号

- 首次向导创建管理员时，会自动把用户名和密码哈希写入 `.env`。
- 升级前已有 SQLite 管理员时，会将原用户名和哈希补存到 `.env`，保留原密码和现有会话。
- “登录账号”页修改账号需验证当前密码。新密码留空可只改用户名；保存立即生效，当前设备获得新会话，其他会话失效。
- 忘记密码时，先停止 SoulBot，在现有 `.env` 设置用户名和新的 `SOULBOT_WEBUI_PASSWORD`，再启动。明文输入优先于文件中旧的哈希，随后会转换为新哈希，旧会话全部失效。无需删除数据库。
- 修改 `.env` 需要重启。重复启动且账号未变时，保留已有会话。

## 反向 WS 与反代地址

`SOULBOT_ONEBOT_REVERSE_WS_URL` 是 **NapCat 的 WebSocket 客户端要连接的 SoulBot 地址**，不是 SoulBot 主动连接 NapCat 的正向地址。正向模式继续使用 `qq.test_url`。

| 场景 | 示例 |
| --- | --- |
| 同机默认接入 | `ws://127.0.0.1:2526/onebot/v11/ws` |
| 局域网 | `ws://SoulBot所在机器的IP:2526/onebot/v11/ws` |
| HTTPS 代理提供 TLS | `wss://bot.example.com/soulbot/qq/ws` |
| 留空 | 按 `web.host` 和 `web.port` 生成 `/onebot/v11/ws` 地址 |

URL 需要接收路径；路径支持字母、数字、下划线、短横线、点和斜线，不接受路径穿越、百分号编码和保留的 `/api`、`/static` 路径。Token 使用独立字段，不能拼在 URL 中。

自定义路径会在重启后接入同一个 OneBot V11 接收器，并执行相同的 Token 和启用状态检查；官方默认路径仍可使用。反代可以保留自定义路径，也可以重写到 `/onebot/v11/ws`。代理需转发 WebSocket Upgrade、Authorization、X-Self-ID 头，并提供所需的 TLS。

这个地址不会自动开启 QQ、改变 `web.host`/`web.port` 或创建反向代理。跨机器时需另行设置监听和网络；`wss://` 由代理终止 TLS，SoulBot 本身仍按原 HTTP 监听启动。双方 Token 需一致。修改接收路径或 QQ 连接参数后应重启。默认路径和连接方向参见 [NoneBot 官方连接说明](https://onebot.adapters.nonebot.dev/docs/guide/setup/)。

## 优先级与备份

账号和反向地址优先读取进程环境变量，然后读取项目 `.env`；反向地址未设置时使用 YAML 配置。设置了进程环境变量的账号或反向地址时，WebUI 会阻止覆盖对应来源，需修改部署环境并重启。

账号没有在环境配置中设置时继续使用向导或原数据库账号。损坏、缺字段或无效的 `.env` 会显示不含配置值的错误，不会清空数据库。API 密钥原有的“进程环境 → .env → data/secrets.json”优先级继续适用。

备份时停止程序并一并保留 `.env`、`config.yaml`、`data/` 和 `emoji/`。`.env` 含密码哈希与密钥，不属于脱敏配置导出的范围。
