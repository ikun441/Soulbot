import asyncio
import json
import time

import httpx

from soulbot.providers.asr.base import ASRError, Transcription


class OpenAICompatibleASRProvider:
    capabilities = frozenset({"asr"})

    def __init__(self, secrets):
        self.secrets, self.transport, self.last_success = secrets, None, 0

    async def transcribe(self, audio, settings):
        key = self.secrets.get("asr_api_key")
        if not key or not settings.model.strip():
            raise ASRError("configuration", "请配置独立 ASR Key 与识别模型")
        fields = {"model": settings.model, "response_format": "json"}
        if settings.language:
            fields["language"] = settings.language
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=settings.timeout_seconds,
                    transport=self.transport,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        settings.base_url + "/audio/transcriptions",
                        headers={"Authorization": "Bearer " + key},
                        data=fields,
                        files={"file": ("audio." + audio.format, audio.data, audio.mime)},
                    ) as response:
                        status = response.status_code
                        if status != 200:
                            if status in {401, 403}:
                                raise ASRError(
                                    "authentication", "识别服务拒绝鉴权，请核对独立 ASR Key"
                                )
                            if status == 429:
                                raise ASRError("rate_limit", "识别服务限流或额度不足")
                            if status in {400, 404, 413, 422}:
                                raise ASRError(
                                    "parameters",
                                    "识别模型、语言、格式或接口不可用，请按服务商文档核对",
                                )
                            raise ASRError("unavailable", "识别服务返回异常，没有自动重试", True)
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 65536:
                                raise ASRError("size", "识别响应过大，已停止接收", True)
            try:
                value = json.loads(content)
                text = value["text"]
                if not isinstance(text, str) or len(text) > 12000:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise ASRError("format", "识别服务没有返回有效 JSON text 字段", True) from None
            self.last_success = time.time()
            # Duration comes from validated input, not untrusted provider metadata.
            return Transcription(text.strip(), settings.language or None, audio.duration)
        except (TimeoutError, httpx.TimeoutException):
            raise ASRError(
                "timeout", "语音转写超时；服务商可能已计费，没有自动重试", True
            ) from None
        except httpx.HTTPError:
            raise ASRError("network", "无法完成识别请求，请检查网络和地址", True) from None

    async def health_check(self):
        return time.time() - self.last_success < 600

    async def close(self):
        pass
