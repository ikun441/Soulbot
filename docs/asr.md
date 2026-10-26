# ASR 语音识别 · v0.8.0

打开“语音识别”，选择本地或云端服务，启用后保存。选择不超过 8MB 的实际 MP3 或 PCM WAV，点击“保存并转写录音”。结果只显示在当前页面，不自动请求聊天模型或发送 QQ。系统不建立独立录音/转写历史；上传文件临时处理后关闭，费用和审计只保存服务、秒数与状态。

## 本地 CPU 识别

使用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 1.2.1 与 [Systran Whisper base 多语言模型](https://huggingface.co/Systran/faster-whisper-base)，均为 MIT 许可。模型固定提交 `ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66`，不下载或执行仓库脚本。相关 Python 包由 start.sh/start.cmd 安装到 `.venv`；模型需要在 WebUI 明确点击准备，约 153MB，保存在忽略的 `data/asr`。保留上游模型说明，不提交权重。

识别使用 CPU int8、4 线程，一个本地任务；每次在独立自有进程加载模型，超时/取消时终止并回收。无须 CUDA、GPU 或系统 FFmpeg，PyAV wheel 提供音频解码库。模型准备完成后，识别设置为离线模式，不上传录音，不获取其它模型或分词器。本机没有云端 ASR API 费用，硬件、电费和下载流量不属于 API 费用统计。

2026-10-26 已在 macOS/Python 3.11 实际安装并下载模型，经认证上传接口转写 3.912 秒的通用 Edge 中文测试 MP3，返回非空中文，记录秒数并清理工作进程。实际输出为“這是SOBOT的人工智能合成與音測試”，存在名称与同音字误差，不代表准确率评估通过。Whisper 可能输出繁体，也可能漏字或在噪音下误识别，重要内容必须核对。可运行 `PYTHONPATH=. .venv/bin/python scripts/verify_asr.py` 复测，先准备本地模型和通用测试 MP3。

## 独立云端接口

填写独立 Base URL、识别模型与 `SOULBOT_ASR_API_KEY`，不复用 LLM/TTS Key，普通读取只显示密钥是否配置。根据 [官方 Transcriptions API](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)，发送 multipart POST `/audio/transcriptions`，包含 file、model、response_format=json 和可选 language。返回必须有有效字符串 text，当前不请求流式、翻译、说话人或其它未经声明能力。

录音语言默认 zh；留空自动识别。云端会接收完整录音，需确认相关人员已授权。价格按人民币/音频分钟填写；按 Token 或其它方式计费请留空，显示未知费用，并核对服务商账单。当前兼容接口已做契约/失败测试，缺少有效 ASR Key，尚未实际调用云端服务。

## QQ 语音

`asr.enabled` 和 `asr.qq_enabled` 都默认关闭。QQ 识别单独授权且需要反向 WebSocket；当前正向驱动的单帧限制不适合大 Base64 音频，配置时会明确拒绝此组合。改变 QQ 连接方式需要重启。

1. 先检查 QQ/私聊/群权限、机器人来源、群静默/禁言与冷却；仅授权私聊和明确 @/回复机器人的群语音进入识别，不逐条识别普通群语音。
2. 去重并持久保留每会话/群的录音处理冷却（至少 3 秒），限制同时取音任务。
3. 只接受消息中一个不含路径或网址的 NapCat 文件标识，调用 get_record(file_id=..., out_format=wav)，读取有大小上限的 Base64。不会读取响应里的 file/url，也不会下载任意网络地址。
4. 格式和时长校验后预留 ASR 额度，再识别。获取后和识别后重新检查授权；撤销授权会停止后续处理。
5. 转写作为带“可能有误”标记的用户内容进入原 Agent，保留机器人/群/用户会话隔离、消息聚合、LLM 预算与发送权限。语音转写不能直接执行 `/soulbot` 管理命令。成功回复后，文本按已有短期上下文策略保留，可在行为页清理。

已阅读固定 NapCat 提交 [3420f0b 的 GetRecord 实现](https://github.com/NapNeko/NapCatQQ/blob/3420f0b76d7dc0ac92cde1663f21ea27f61e13bb/packages/napcat-onebot/action/file/GetRecord.ts)，确认转换格式和 Base64 返回；适配器协议、权限撤销、去重/冷却已测试。实际登录 QQ 的录音收取、NapCat 音频转换与 LLM 回复链路仍待实机联调；接口不支持时记录失败，不伪造识别成功。

## 额度、取消与升级

默认单段 60 秒、超时 90 秒，每分钟 3 次、每天 50 次/1800 音频秒数；可在界面调整至硬性上限。云端最多两个任务，本地一个。调用和秒数持久化，失败尝试也占频率/秒数；Token、TTS 字符与 ASR 秒数分开，已知费用合并检查同一每日人民币预算。

取消、超时、崩溃前未结算的云端请求可能已经计费，记录保守保留为“消耗未确认”，不自动重试。识别异常不影响核心管理系统。

启动升级 SQLite schema 5→6，仅增加 asr_usage，不清空账户、会话、TTS 或聊天用量。升级前备份 `.env`、`config.yaml` 和 `data`；旧版本不能直接打开 schema 6，回退需恢复升级前数据库备份。Linux/Windows 的原生依赖、完整浏览器上传和真实 QQ 链路仍需对应环境验收。
