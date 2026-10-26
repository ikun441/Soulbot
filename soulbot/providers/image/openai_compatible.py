import asyncio
import base64
import binascii
import json
import time

import httpx

from soulbot.providers.image.base import GeneratedImage, ImageGenerationError
from soulbot.services.media_service import MAX_IMAGE_BYTES, MediaError, inspect_image

MAX_BASE64_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4
MAX_RESPONSE_BYTES = MAX_BASE64_CHARS + 65536


class OpenAICompatibleImageProvider:
    capabilities = frozenset({"image_generation"})

    def __init__(self, secrets):
        self.secrets, self.transport, self.last_success = secrets, None, 0

    async def generate(self, prompt, settings):
        key = self.secrets.get("image_api_key")
        if not key or not settings.model.strip():
            raise ImageGenerationError("configuration", "请填写独立生图 Key 和模型 ID")
        body = {"model": settings.model, "prompt": prompt, "n": 1}
        for name in ("size", "quality"):
            if value := getattr(settings, name):
                body[name] = value
        for name in ("response_format", "output_format"):
            if (value := getattr(settings, name)) != "omit":
                body[name] = value
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=settings.timeout_seconds,
                    follow_redirects=False,
                    transport=self.transport,
                ) as client:
                    async with client.stream(
                        "POST",
                        settings.base_url + "/images/generations",
                        headers={"Authorization": "Bearer " + key},
                        json=body,
                    ) as response:
                        status = response.status_code
                        if status != 200:
                            if status in {401, 403}:
                                raise ImageGenerationError(
                                    "authentication", "生图服务拒绝鉴权，请核对独立图片 Key"
                                )
                            if status == 429:
                                raise ImageGenerationError("rate_limit", "生图服务限流或额度不足")
                            if status in {400, 404, 413, 422}:
                                raise ImageGenerationError(
                                    "parameters",
                                    "模型、提示词或可选参数被服务拒绝，请按服务商文档核对",
                                )
                            raise ImageGenerationError(
                                "unavailable", "生图服务返回异常，没有自动重试", True
                            )
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_RESPONSE_BYTES:
                                raise ImageGenerationError(
                                    "size", "生图响应超过单张图片大小限制，已停止接收", True
                                )
            try:
                value = json.loads(content)
                records = value["data"]
                if (
                    not isinstance(records, list)
                    or len(records) != 1
                    or not isinstance(records[0], dict)
                ):
                    raise ValueError
                encoded = records[0].get("b64_json")
                if not encoded and records[0].get("url"):
                    raise ImageGenerationError(
                        "url_only",
                        "服务只返回了图片 URL，未下载；请按服务能力切换 Base64 返回方式，服务商可能已计费",
                        True,
                    )
                if not isinstance(encoded, str) or len(encoded) > MAX_BASE64_CHARS:
                    raise ValueError
                data = base64.b64decode(encoded, validate=True)
                _, mime, width, height = await asyncio.to_thread(inspect_image, data)
            except ImageGenerationError:
                raise
            except (ValueError, TypeError, KeyError, binascii.Error, MediaError):
                raise ImageGenerationError(
                    "format", "生图服务没有返回有效的单张 Base64 图片；可能已经计费", True
                ) from None
            self.last_success = time.time()
            return GeneratedImage(data, mime, width, height)
        except (TimeoutError, httpx.TimeoutException):
            raise ImageGenerationError(
                "timeout", "生图超时；服务商可能已计费，没有自动重试", True
            ) from None
        except httpx.HTTPError:
            raise ImageGenerationError(
                "network", "无法完成生图请求，请检查服务地址和网络", True
            ) from None

    async def health_check(self):
        return time.time() - self.last_success < 600

    async def close(self):
        pass
