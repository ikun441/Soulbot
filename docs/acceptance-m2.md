# M2 首个增量验收 · v0.3.0

日期：2026-10-25。M2 尚未全部完成；本次范围为 Emoji、meme-generator、图片产物和有限媒体工具调用。

## 自动化回归

- 66 项 pytest 通过，覆盖旧 M0/M1 与新的中文递归表情索引、标签/别名、分页、删除后重建、上传、私有预览、符号链接/路径穿越拦截。
- 图片产物格式、尺寸、大小、过期与跨 QQ 会话隔离；工具权限撤销后禁止发送，模型请求与工具 Schema 共享任务预算。
- 模板 multipart 请求、元数据图片/文字数量、外部 Schema 引用拒绝、单实例文件锁、第三方未准备时核心仍可用。
- OneBot 图片消息段使用真实安装适配器的 base64 编码；测试发送只能给主人，必须显式确认且每分钟限一次。QQ Bot 回执在此处为夹具，未声称实际 QQ 接收成功。
- Ruff lint/格式、ES Modules JavaScript 语法、Bash 语法、pip check 与 Git diff 检查通过。
- 依赖存在一条 Starlette TestClient/httpx 弃用警告，测试结果未受影响。

## 实际第三方验收

环境：macOS arm64 / Python 3.11，独立安装 meme-generator 0.1.14 / Pillow10.4 / pil-utils0.2.2 / skia-python144.1。源码没有复制进 SoulBot。

`scripts/verify_meme.py` 实际执行隔离安装/资源校验、读取 `/meme/version`、295 个模板、raise_sign 参数与预览、690×690 JPEG、112×112 petpet GIF。2,869 个官方素材按清单校验，中文字体实际渲染正常。

同一验收还验证了输入数量不符拒绝、另一实例无法取得锁、仅杀死本次创建的服务模拟崩溃后恢复到新进程，以及结束时释放进程/文件锁。结果在忽略的 output/m2-live/result.json；未使用模拟第三方返回值。

## 真实 HTTP 与界面

使用 output/acceptance 隔离配置、127.0.0.1:2527 管理端连接本机真实 2233 API，经登录会话完成模板搜索、详情、预览、JPEG 生成和下载、中文文件名 Emoji 上传、别名保存与检索。错误文字数量返回 422。记录保存在忽略的 output/m2-http-result.json。

Edge 已实际打开并读取 v0.3.0 仪表盘/工坊界面。后续浏览器自动化多次报告人工操作/超时，完整按钮点击生成流程尚未确认；没有把 HTTP 验证写成完整浏览器验收。正式配置未被验收配置覆盖。

## 待验收与后续范围

- 正式运行配置尚无 QQ Access Token / LLM API Key；需要真实 NapCat 登录与模型账户继续验证图片接收和模型工具兼容性。
- Linux/Windows 和其它 Python 版本的第三方原生依赖尚未实机测试。
- TTS/ASR、云端生图、HTML Render、QQ 来图理解与其它后续能力不计入本次完成范围。
- 发布 Commit、v0.3.0 Tag 与推送状态以实际 Git 引用和本次开发汇报为准。
