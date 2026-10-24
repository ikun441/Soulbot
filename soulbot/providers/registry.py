from soulbot.providers.base import Provider


class ProviderRegistry:
    def __init__(self):
        self.providers: dict[tuple[str, str], Provider] = {}
        self.defaults: dict[str, str] = {}

    def register(self, capability: str, name: str, provider: Provider, default=False):
        if capability not in provider.capabilities:
            raise ValueError("Provider 不支持该能力")
        key = capability, name
        if key in self.providers:
            raise ValueError("Provider 重复注册")
        self.providers[key] = provider
        if default or capability not in self.defaults:
            self.defaults[capability] = name

    def resolve(self, capability: str, name: str | None = None):
        selected = name or self.defaults.get(capability)
        if (capability, selected) not in self.providers:
            raise LookupError("尚未注册该能力的 Provider")
        return self.providers[capability, selected]

    async def close(self):
        import asyncio

        unique = {id(provider): provider for provider in self.providers.values()}
        await asyncio.gather(*(p.close() for p in unique.values()), return_exceptions=True)
