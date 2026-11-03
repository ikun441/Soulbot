"""Bounded QQ public-avatar reads from a fixed endpoint."""

import re

import httpx

from soulbot.services.media_service import MediaError, inspect_image


async def avatar(qq):
    if not re.fullmatch(r"[1-9][0-9]{4,11}", qq):
        raise MediaError("头像目标不是本次消息提供的有效 QQ")
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
            async with client.stream(
                "GET", "https://q1.qlogo.cn/g", params={"b": "qq", "nk": qq, "s": "640"}
            ) as response:
                if response.status_code != 200:
                    raise MediaError("公开头像暂时不可用，请稍后重试")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise MediaError("公开头像超过 2MB，已停止读取")
        inspect_image(bytes(content))
        return bytes(content)
    except httpx.HTTPError:
        raise MediaError("读取 QQ 公开头像失败，没有使用任意网址或本地文件") from None
