# 部署说明 · v0.3.0

推荐先使用 start.md 本机部署。默认 127.0.0.1:2526，Python 3.11–3.13，单进程运行；本阶段只在 macOS 实际验证。避免多 worker：NoneBot 连接和配置内存快照当前为单实例设计。

首次运行自动生成 data、logs、config.yaml，并自动初始化 SQLite。脚本只安装项目 .venv 中的 Python 包；不执行系统级服务安装。NapCat 独立安装。启用 meme-generator 后，在 third_party/meme-generator/.venv 准备固定上游版本和素材；启动与恢复在后台进行，见 [多模态部署](multimodal.md)。

## 高级网络部署

当前推荐只在本机管理。若需局域网：先在本机创建管理员，停止后将 config.yaml 的 web.host 设为 0.0.0.0，然后设置环境变量 `SOULBOT_ALLOWED_HOSTS` 为自己的实际主机名或 IP（逗号分隔）后启动。否则 Host 检查会拒绝该地址。反向 WS 的客户端地址使用 SoulBot IP，双方 Token 一致。

公网需由维护者提供 HTTPS 反向代理、可信网络和访问控制，不应直接暴露管理端口。使用 localhost 的 SSH 端口转发是远程管理的简便选项。生产网络部署与 Linux/Windows 跨平台验收尚未完成。WebUI 和 OneBot 共用同一监听端口，请限制其网络访问。

## 依赖与更新

requirements.lock 记录本次运行中验证的版本，bootstrap 使用约束安装。依赖文件指纹变化会自动重新检查并安装。requirements-dev.txt 用于开发测试，不是运行前提。

配置无效时不重置文件。更新前先停止、完整备份 data/config/.env，使用 git pull --ff-only；不要用 reset --hard 覆盖本地运行数据。
