from dataclasses import dataclass
from typing import Protocol

from soulbot.services.config_service import ImageGenerationSettings
from soulbot.services.media_service import MediaError


class ImageGenerationError(MediaError):
    def __init__(self, kind, message, uncertain=False):
        super().__init__(message)
        self.kind, self.uncertain = kind, uncertain


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime: str
    width: int
    height: int


class ImageGenerationProvider(Protocol):
    capabilities: frozenset[str]

    async def generate(self, prompt: str, settings: ImageGenerationSettings) -> GeneratedImage: ...

    async def health_check(self) -> bool: ...

    async def close(self) -> None: ...
