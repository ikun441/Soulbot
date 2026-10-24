from typing import Protocol


class Provider(Protocol):
    capabilities: frozenset[str]

    async def health_check(self) -> bool: ...

    async def close(self) -> None: ...
