import sys
from pathlib import Path

import yaml

from soulbot.adapters.onebot_v11 import create_runtime
from soulbot.app import build_services
from soulbot.services.environment_service import EnvironmentError


def main():
    import os

    from soulbot import VERSION

    root = Path(os.environ.get("SOULBOT_HOME", Path(__file__).resolve().parent)).resolve()
    try:
        services = build_services(root)
        if "--check" in sys.argv:
            print("配置与数据库初始化检查通过")
            return
        server = create_runtime(services)
        settings = services.running_config
        host = "127.0.0.1" if settings.web.host == "0.0.0.0" else settings.web.host
        print(f"SoulBot {VERSION} · M4 本地日记与调度", flush=True)
        print(f"WebUI：http://{host}:{settings.web.port}  |  Ctrl+C 停止", flush=True)
        print("人格与情绪属于程序模拟。配置模型和 NapCat 后即可进行文本聊天。", flush=True)
        server.run()
    except KeyboardInterrupt:
        print("SoulBot 已停止。")
    except EnvironmentError as exc:
        print(f"启动失败：{exc}")
        raise SystemExit(1) from None
    except (ValueError, OSError, yaml.YAMLError) as exc:
        # Do not print raw exception content: malformed config may contain secrets.
        print(f"启动失败（{type(exc).__name__}）：请检查 config.yaml、端口占用和 data 目录权限。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
