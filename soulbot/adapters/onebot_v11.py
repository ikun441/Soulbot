"""NoneBot2/OneBot V11 transport. Core code never imports framework events."""

import asyncio
import base64
import hashlib
import re
import secrets
from urllib.parse import urlsplit

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
from soulbot.services.audio_service import MAX_AUDIO_BYTES, AudioPayload
from soulbot.services.media_service import ImagePayload, MediaError


def normalize(event: MessageEvent, known_bot_ids=()):
    bot_id, user_id = str(event.self_id), str(event.user_id)
    mentions = [
        str(segment.data.get("qq", ""))
        for segment in event.original_message
        if segment.type == "at"
    ]
    reply = "none"
    recordings = [segment for segment in event.original_message if segment.type == "record"]
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
        record_file=str(recordings[0].data.get("file", "")) if len(recordings) == 1 else None,
    )


class NoneBotTransport:
    async def fetch_record(self, event):
        # Current NapCat emits an opaque UUID. Never send arbitrary URLs/paths to it.
        if not event.record_file or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,180}(?:\.(?:amr|silk|mp3|wav))?", event.record_file
        ):
            raise MediaError("录音标识不是受支持的 NapCat 文件 ID，已拒绝读取路径或网址")
        try:
            async with asyncio.timeout(20):
                result = await nonebot.get_bot(event.bot_id).call_api(
                    "get_record", file_id=event.record_file, out_format="wav"
                )
            content = result.get("base64") if isinstance(result, dict) else None
            if not isinstance(content, str) or len(content) > ((MAX_AUDIO_BYTES + 2) // 3) * 4:
                raise ValueError
            return base64.b64decode(content, validate=True)
        except Exception:
            raise MediaError(
                "NapCat 未返回受支持的录音 Base64；请检查 get_record、FFmpeg 与文件是否仍有效"
            ) from None

    @staticmethod
    def connected():
        return sorted(nonebot.get_bots())

    async def __call__(self, event, text):
        bot = nonebot.get_bot(event.bot_id)
        message = (
            MessageSegment.record("base64://" + base64.b64encode(text.data).decode())
            if isinstance(text, AudioPayload)
            else MessageSegment.image("base64://" + base64.b64encode(text.data).decode())
            if isinstance(text, ImagePayload)
            else MessageSegment.text(text)
        )
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
        reverse_path = urlsplit(
            self.services.running_config.qq.receiver_url(self.services.running_config.web)
        ).path
        custom_reverse = scope["type"] == "websocket" and scope.get("path") == reverse_path
        if scope["type"] in {"websocket", "http"} and (
            scope.get("path", "").startswith("/onebot/") or custom_reverse
        ):
            current = getattr(self.services, "config", None)
            tokenless_remote = getattr(
                self.services, "running_token_digest", None
            ) == hashlib.sha256(b"").hexdigest() and (scope.get("client") or ("", 0))[0] not in {
                "127.0.0.1",
                "::1",
            }
            if (
                not self.services.running_config.qq.enabled
                or tokenless_remote
                or (current and not current.current.qq.enabled)
            ):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await send({"type": "http.response.start", "status": 403, "headers": []})
                    await send({"type": "http.response.body", "body": b"QQ adapter disabled"})
                return
            if custom_reverse:
                scope = {**scope, "path": "/onebot/v11/ws", "raw_path": b"/onebot/v11/ws"}
        await self.app(scope, receive, send)


def create_runtime(services):
    settings = services.running_config
    token = services.secrets.get("onebot_access_token")
    if settings.qq.enabled and not token and settings.qq.requires_token(settings.web):
        raise ValueError("非本机 QQ 接入需要 Access Token，请先在 WebUI 中设置")
    urls = (
        [settings.qq.test_url]
        if settings.qq.enabled and settings.qq.connection == "forward"
        else []
    )
    nonebot.init(
        driver="~fastapi+~httpx+~websockets",
        host=settings.web.host,
        port=settings.web.port,
        onebot_access_token=token or (None if settings.qq.enabled else secrets.token_urlsafe(32)),
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
            normalized = normalize(event, transport.connected())
            if normalized.record_file is not None:
                await services.agent.accept_record(normalized)
            else:
                await services.agent.accept(normalized)
        except Exception:
            # Do not let framework exception logging include private event text.
            await asyncio.to_thread(services.db.audit, "event_failed", "internal")

    web = create_web_app(services.root, services)
    nonebot.get_app().mount("/", web)

    async def shutdown():
        await services.meme.close()
        await services.agent.close()
        await services.speech.close()
        await services.recognition.close()
        await services.images.close()
        await services.providers.close()

    driver.on_shutdown(shutdown)
    driver.on_startup(services.meme.startup)
    import uvicorn

    config = uvicorn.Config(
        OneBotGate(nonebot.get_asgi(), services),
        host=settings.web.host,
        port=settings.web.port,
        log_level="warning",
        access_log=False,
    )
    return uvicorn.Server(config)
