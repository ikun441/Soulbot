import asyncio
import re
import time

import edge_tts

from soulbot.providers.tts.base import SpeechError
from soulbot.services.audio_service import MAX_AUDIO_BYTES, inspect_audio


class EdgeTTSProvider:
    capabilities = frozenset({"tts", "voice_listing", "rate", "pitch", "volume"})

    def __init__(self):
        self.cache, self.cache_at = [], 0
        self.lock = asyncio.Lock()

    async def voices(self, timeout=20, refresh=False):
        async with self.lock:
            if self.cache and not refresh and time.time() - self.cache_at < 600:
                return self.cache
            try:
                async with asyncio.timeout(timeout):
                    data = await edge_tts.list_voices()
                if not isinstance(data, list) or not 1 <= len(data) <= 2000:
                    raise ValueError
                result = [
                    {
                        "id": item["ShortName"],
                        "name": str(item.get("FriendlyName", item["ShortName"]))[:200],
                        "locale": item["Locale"],
                        "gender": item.get("Gender", ""),
                    }
                    for item in data
                    if re.fullmatch(r"[a-zA-Z0-9_\-]{1,100}", item.get("ShortName", ""))
                ]
                if not result:
                    raise ValueError
                self.cache, self.cache_at = result, time.time()
                return result
            except Exception:
                raise SpeechError(
                    "unavailable",
                    "无法读取 Edge 实际音色，请检查网络后重试；该接入方式为非官方服务",
                ) from None

    async def synthesize(self, text, settings):
        voice = settings.edge.voice
        voices = await self.voices(min(settings.timeout_seconds, 20))
        if not any(item["id"] == voice for item in voices):
            raise SpeechError("voice", "所选音色不在 Edge 实际列表中，请重新读取并选择")
        try:
            content = bytearray()
            async with asyncio.timeout(settings.timeout_seconds):
                stream = edge_tts.Communicate(
                    text,
                    voice,
                    rate=f"{settings.edge.rate_percent:+d}%",
                    pitch=f"{settings.edge.pitch_hz:+d}Hz",
                    volume=f"{settings.edge.volume_percent:+d}%",
                    connect_timeout=min(int(settings.timeout_seconds), 10),
                    receive_timeout=int(settings.timeout_seconds),
                )
                async for chunk in stream.stream():
                    if chunk["type"] == "audio":
                        content.extend(chunk["data"])
                        if len(content) > MAX_AUDIO_BYTES:
                            raise SpeechError("size", "语音结果超过 8MB，已停止接收", True)
            return await asyncio.to_thread(inspect_audio, bytes(content))
        except asyncio.CancelledError:
            raise
        except SpeechError:
            raise
        except TimeoutError:
            raise SpeechError("timeout", "Edge 语音生成超时，没有自动重试合成请求", True) from None
        except Exception:
            raise SpeechError(
                "unavailable", "Edge 未返回有效语音，请检查网络、音色或稍后重试", True
            ) from None

    async def health_check(self):
        try:
            return bool(await self.voices())
        except SpeechError:
            return False

    async def close(self):
        self.cache = []
