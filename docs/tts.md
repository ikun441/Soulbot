# TTS 语音工坊 · v0.7.0

在侧栏打开“语音工坊”，启用语音并选择服务，保存后生成一段文字试听。生成按钮就是实际合成测试，会消耗调用/字符额度，云端服务可能计费。生成结果可在浏览器播放、下载，或确认后发给主人 QQ。音频保留 24 小时，删除过期产物在启动和生成时执行。

## Edge TTS

使用 [edge-tts 官方项目](https://github.com/rany2/edge-tts) 7.3.1，这是非官方接入，可能限流或改变兼容性。无需密钥，文字会发往微软语音服务。点击“读取实际音色”查询服务返回值，缓存 10 分钟；默认 zh-CN-XiaoxiaoNeural 已实际验证。可以调整语速、音调、音量。返回格式为 MP3，解析实际文件得到采样率、声道和时长。

2026-10-26 真实验证：322 个音色；认证 API 生成 24 kHz 单声道 MP3（约 4.46 秒），受保护读取成功；真实 synthesize_speech 工具生成及会话隔离检查通过。`PYTHONPATH=. .venv/bin/python scripts/verify_tts.py` 可复测，数据保存在忽略的 output/tts-live，测试不会发送 QQ。

## OpenAI 兼容接口

独立填写 Base URL、模型和音色以及语音 API Key。密钥保存为 `SOULBOT_TTS_API_KEY`，不会自动复用聊天模型密钥，也不会通过普通配置接口回显。接口依据 [官方 Speech API](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)：POST `/audio/speech`，发送 model、input、voice、response_format、speed。按服务商实际支持选择 MP3 或 PCM WAV。instructions 需要显式声明服务支持，不猜测模型能力，不假设存在统一音色查询接口。

价格字段单位为人民币/千字符，只用于已确认按字符计费的服务；按 Token 或其它方式计费时留空并核对账单。未知价格显示未知，暂停主动任务。这里记录的是按用户报价估算的费用，不是服务商账单。当前兼容接口已通过契约、鉴权、限流、错误格式等测试，尚未使用有效云端 Key 实际联调。

## 额度与发送

- 默认关闭；单次最多 500 字符、120 秒，最大 8MB；并发最多 2 个任务。
- 默认每分钟 3 次、每天 50 次、每天 10000 字符。失败尝试也计次数和字符，重启不清零。
- 费用与 LLM 共享每日人民币预算，字符数与 Token 分别统计。超时、取消和进程中断保留不确定消耗；不自动重试可能已计费的合成请求。
- 服务端验证音频内容与元数据，不凭扩展名或 HTTP Content-Type 猜格式。文件权限 0600，管理端需要认证，模型工具产物限制在其服务端会话范围。
- 每次模型任务最多合成 1 段语音；发送时再检查工具权限、语音开关、QQ 接入和会话权限，遵守全局频率。
- 主人测试发送需确认，每分钟最多一次。先单独发送“【AI 合成语音】”，再发送独立 OneBot `record`。只收到有效 message_id 才报告平台确认；仍需在 QQ 检查实际播放。

NapCat 的音频转换依赖其版本与 FFmpeg/SILK 能力。当前已验证适配器发送独立 record 与回执处理，实际登录 QQ 的播放链路尚未联调；不会虚报已播放。网页完整试听流程因本次浏览器自动化超时尚待补验，认证 API 及费用页面已验证。

## 升级

启动时将 SQLite schema 4 升级到 5，新增 audio_artifacts 和 tts_usage，不清空账号、会话、聊天或预算记录。升级前备份 `.env`、`config.yaml` 和 `data`。不要用旧版直接打开 schema 5 数据库；回退需恢复升级前数据库备份。依赖始终安装在项目 `.venv`，无需系统服务。
