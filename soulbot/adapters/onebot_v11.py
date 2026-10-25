"""NoneBot2/OneBot V11 transport. Core code never imports framework events."""

import asyncio
import secrets

import nonebot
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
)

from soulbot.app import create_web_app
from soulbot.core.events import ChatEvent


def normalize(event: MessageEvent, known_bot_ids=()):
    bot_id, user_id = str(event.self_id), str(event.user_id)
    mentions = [
        str(segment.data.get("qq", ""))
        for segment in event.original_message
        if segment.type == "at"
    ]
    reply = "none"
    if event.reply and event.reply.sender.user_id is not None:
        reply = "bot" if str(event.reply.sender.user_id) == bot_id else "other"
    return ChatEvent(
        bot_id=bot_id,
        user_id=user_id,
        message_id=str(event.message_id),
        text=event.message.extract_plain_text().strip(),
        group_id=str(event.group_id) if isinstance(event, GroupMessageEvent) else None,
        directed=event.is_tome() or bot_id in mentions,
        reply_target=reply,
        mentions_other=any(value not in {bot_id, "all", ""} for value in mentions),
        is_bot=user_id in known_bot_ids or bool(getattr(event.sender, "is_bot", False)),
        role=event.sender.role or "member",
    )


class NoneBotTransport:
    @staticmethod
    def connected():
        return sorted(nonebot.get_bots())

    async def __call__(self, event, text):
        bot = nonebot.get_bot(event.bot_id)
        message = MessageSegment.text(text)
        if event.group_id:
            result = await bot.send_group_msg(group_id=int(event.group_id), message=message)
        else:
            result = await bot.send_private_msg(user_id=int(event.user_id), message=message)
        if not isinstance(result, dict) or "message_id" not in result:
            raise ValueError("OneBot 未返回有效发送回执")

    async def status(self):
        states = []
        for bot_id in self.connected():
            try:
                async with asyncio.timeout(5):
                    status = await nonebot.get_bot(bot_id).get_status()
                online = status.get("online") is True and status.get("good") is True
                states.append({"bot_id": bot_id, "online": online})
            except Exception:
                states.append({"bot_id": bot_id, "online": False})
        ok = bool(states) and all(item["online"] for item in states)
        return {
            "ok": ok,
            "bots": states,
            "message": "NapCat 已实际响应，QQ 在线"
            if ok
            else "适配器已连接，但 QQ 状态未确认；请检查 NapCat 登录",
        }


class OneBotGate:
    def __init__(self, app, services):
        self.app, self.services = app, services

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"websocket", "http"} and scope.get("path", "").startswith("/onebot/"):
            current = getattr(self.services, "config", None)
            if not self.services.running_config.qq.enabled or (
                current and not current.current.qq.enabled
            ):
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
    transport = NoneBotTransport()
    services.qq_transport = transport
    services.agent.executor.sender = transport
    matcher = nonebot.on_message(priority=50, block=False)

    @matcher.handle()
    async def receive_message(bot: Bot, event: MessageEvent):
        try:
            await services.agent.accept(normalize(event, transport.connected()))
        except Exception:
            # Do not let framework exception logging include private event text.
            await asyncio.to_thread(services.db.audit, "event_failed", "internal")

    web = create_web_app(services.root, services)
    nonebot.get_app().mount("/", web)

    async def shutdown():
        await services.agent.close()
        await services.providers.close()

    driver.on_shutdown(shutdown)
    import uvicorn

    config = uvicorn.Config(
        OneBotGate(nonebot.get_asgi(), services),
        host=settings.web.host,
        port=settings.web.port,
        log_level="warning",
        access_log=False,
    )
    return uvicorn.Server(config)
