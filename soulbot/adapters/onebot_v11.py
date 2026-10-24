"""Bootstrap genuine NoneBot driver/adapter, leaving message handling to M1."""

import secrets

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

from soulbot.app import create_web_app


class OneBotGate:
    def __init__(self, app, services):
        self.app, self.services = app, services

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"websocket", "http"} and scope.get("path", "").startswith("/onebot/"):
            if not self.services.running_config.qq.enabled:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 403, "headers": []})
                    await send({"type": "http.response.body", "body": b"QQ adapter disabled"})
                return
        await self.app(scope, receive, send)


def create_runtime(services):
    settings = services.running_config
    token = services.secrets.get("onebot_access_token")
    if settings.qq.enabled and not token:
        raise ValueError("启用 QQ 适配器需要 Access Token，请先在 WebUI 中设置")
    urls = (
        [settings.qq.test_url]
        if settings.qq.enabled and settings.qq.connection == "forward"
        else []
    )
    # Read secrets explicitly; never let unrelated .env files configure the driver.
    nonebot.init(
        driver="~fastapi+~httpx+~websockets",
        host=settings.web.host,
        port=settings.web.port,
        onebot_access_token=token or secrets.token_urlsafe(32),
        onebot_ws_urls=urls,
        log_level="WARNING",
        _env_file=None,
    )
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    services.qq_runtime = True
    web = create_web_app(services.root, services)
    # Adapter routes must be matched before the catch-all administration mount.
    nonebot.get_app().mount("/", web)
    driver.on_shutdown(services.providers.close)
    import uvicorn

    config = uvicorn.Config(
        OneBotGate(nonebot.get_asgi(), services),
        host=settings.web.host,
        port=settings.web.port,
        log_level="warning",
        access_log=False,
    )
    return uvicorn.Server(config)
