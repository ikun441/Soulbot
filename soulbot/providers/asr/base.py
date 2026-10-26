from dataclasses import dataclass
from typing import Protocol

from soulbot.services.audio_service import AudioPayload
from soulbot.services.config_service import ASRSettings
from soulbot.services.media_service import MediaError


class ASRError(MediaError):
    def __init__(self, kind, message, uncertain=False):
        super().__init__(message)
        self.kind, self.uncertain = kind, uncertain


@dataclass(frozen=True)
class Transcription:
    text: str
    language: str | None
    duration: float


class ASRProvider(Protocol):
    capabilities: frozenset[str]

    async def transcribe(self, audio: AudioPayload, settings: ASRSettings) -> Transcription: ...

    async def health_check(self) -> bool: ...

    async def close(self) -> None: ...
