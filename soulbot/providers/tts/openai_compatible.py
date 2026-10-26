import asyncio
import time

import httpx

from soulbot.providers.tts.base import SpeechError
from soulbot.services.audio_service import MAX_AUDIO_BYTES, inspect_audio


class OpenAICompatibleTTSProvider:
    capabilities = frozenset({"tts"})

    def __init__(self, secrets):
        self.secrets, self.transport, self.last_success = secrets, None, 0

    async def synthesize(self, text, settings):
        endpoint = settings.compatible
        key = self.secrets.get("tts_api_key")
        if not key or not endpoint.model or not endpoint.voice:
            raise SpeechError("not_configured", "请先配置语音服务的独立 Key、模型和音色")
        body = {
            "model": endpoint.model,
            "input": text,
            "voice": endpoint.voice,
            "response_format": endpoint.response_format,
            "speed": endpoint.speed,
        }
        if endpoint.supports_instructions and endpoint.instructions:
            body["instructions"] = endpoint.instructions
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=settings.timeout_seconds,
                    follow_redirects=False,
                    transport=self.transport,
                ) as client:
                    async with client.stream(
                        "POST",
                        endpoint.base_url + "/audio/speech",
                        headers={"Authorization": "Bearer " + key},
                        json=body,
                    ) as response:
                        if response.status_code != 200:
                            status = response.status_code
                            if status in {401, 403}:
                                raise SpeechError(
                                    "authentication", "语音服务拒绝鉴权，请核对独立 TTS Key"
                                )
                            if status == 429:
                                raise SpeechError(
                                    "rate_limit", "语音服务限流或额度不足，请稍后重试"
                                )
                            if status in {400, 404, 413, 422}:
                                raise SpeechError(
                                    "parameters",
                                    "语音模型、音色、参数或接口不可用，请按服务商文档核对",
                                )
                            raise SpeechError("unavailable", "语音服务返回异常，没有自动重试", True)
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_AUDIO_BYTES:
                                raise SpeechError("size", "语音结果超过 8MB，已停止接收", True)
            try:
                audio = await asyncio.to_thread(inspect_audio, bytes(content))
                if audio.format != endpoint.response_format:
                    raise ValueError
            except Exception:
                raise SpeechError(
                    "format", "语音服务返回格式与请求不符或不是有效音频", True
                ) from None
            self.last_success = time.time()
            return audio
        except (TimeoutError, httpx.TimeoutException):
            raise SpeechError(
                "timeout", "语音生成超时，没有自动重试；请核对服务商账单", True
            ) from None
        except httpx.HTTPError:
            raise SpeechError("network", "无法完成语音服务请求，请检查地址和网络", True) from None

    async def health_check(self):
        return time.time() - self.last_success < 600

    async def close(self):
        pass
