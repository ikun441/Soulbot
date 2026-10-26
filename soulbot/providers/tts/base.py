from typing import Protocol

from soulbot.services.audio_service import AudioPayload
from soulbot.services.config_service import SpeechSettings
from soulbot.services.media_service import MediaError


class SpeechError(MediaError):
    def __init__(self, kind, message, uncertain=False):
        super().__init__(message)
        self.kind, self.uncertain = kind, uncertain


class TTSProvider(Protocol):
    capabilities: frozenset[str]

    async def synthesize(self, text: str, settings: SpeechSettings) -> AudioPayload: ...

    async def health_check(self) -> bool: ...

    async def close(self) -> None: ...
