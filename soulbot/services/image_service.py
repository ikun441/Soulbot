import asyncio
import time
import uuid

from soulbot.providers.image.base import ImageGenerationError
from soulbot.providers.image.comfyui import ComfyUIProvider
from soulbot.providers.image.openai_compatible import OpenAICompatibleImageProvider
from soulbot.services.media_routing import MediaRouter
from soulbot.services.provider_diagnostics import ProviderDiagnostics


class ImageGenerationService:
    def __init__(self, config, secrets, registry, budget, db, diagnostics=None):
        self.config, self.secrets, self.registry, self.budget, self.db = (
            config,
            secrets,
            registry,
            budget,
            db,
        )
        registry.register(
            "image_generation",
            "openai_compatible",
            OpenAICompatibleImageProvider(secrets),
            default=True,
        )
        registry.register("image_generation", "comfyui", ComfyUIProvider())
        self.jobs, self.closed = set(), False
        self.health = diagnostics or ProviderDiagnostics()
        self.router = MediaRouter(self, "image_generation", ImageGenerationError, self.health)
        for name in ("fallback_1", "fallback_2"):
            registry.register(
                "image_generation", name, OpenAICompatibleImageProvider(secrets, "image_" + name)
            )

    def diagnostics(self):
        policy = self.config.current.image_generation
        return self.health.status(
            "image_generation",
            policy,
            self.router.credentials(policy),
            enabled=policy.enabled,
            configured=self.configured(),
        )

    def record_test(self, policy, credentials, *, ok, error_kind=None):
        self.health.record("image_generation", policy, credentials, ok=ok, error_kind=error_kind)

    def configured(self):
        return self.router.configured()

    def max_runtime(self):
        policy = self.config.current.image_generation
        return min(
            policy.total_timeout_seconds,
            sum(p.timeout_seconds for _, p in self.router.endpoints(policy)),
        )

    def needs_english(self):
        return any(
            p.provider == "comfyui" and p.comfy.tool_prompt_language == "en"
            for _, p in self.router.endpoints(self.config.current.image_generation)
        )

    def reserve(self, settings, purpose, *, route=None):
        policy, now = settings.image_generation, time.time()
        day = self.budget.day(settings)
        cost = policy.price_cny_per_image
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT COALESCE(SUM(images),0) FROM image_usage WHERE day=?", (day,)
            ).fetchone()[0]
            recent = db.execute(
                "SELECT COUNT(*) FROM image_usage WHERE created_at>?", (now - 60,)
            ).fetchone()[0]
            if count >= policy.max_images_per_day or recent >= policy.max_calls_per_minute:
                raise ImageGenerationError("quota", "生图频率或今日图片额度已达上限")
            key = uuid.uuid4().hex
            db.execute(
                "INSERT INTO image_usage VALUES (?,?,?,?,?,'pending',1,?,?,NULL)",
                (
                    key,
                    day,
                    policy.provider,
                    policy.comfy.checkpoint if policy.provider == "comfyui" else policy.model,
                    purpose,
                    cost,
                    now,
                ),
            )
            self.router.reserve_details(db, key, route)
            self.budget.record_warnings(db, settings, day=day)
        return key

    def finish(self, key, status, kind=None):
        with self.db.connect() as db:
            db.execute(
                "UPDATE image_usage SET status=?,error_kind=? WHERE id=?", (status, kind, key)
            )
            if status == "rejected":
                db.execute("UPDATE image_usage SET cost_cny=0 WHERE id=?", (key,))

    async def generate(self, prompt, purpose="web_test", *, only=None):
        settings = self.config.current.model_copy(deep=True)
        policy = settings.image_generation
        prompt = prompt.strip()
        if self.closed or not policy.enabled:
            raise ImageGenerationError("disabled", "图片生成尚未启用")
        if not prompt or len(prompt) > policy.max_prompt_chars:
            raise ImageGenerationError("input", f"提示词需要 1–{policy.max_prompt_chars} 个字符")
        if not self.configured():
            raise ImageGenerationError(
                "configuration",
                "请先配置所选生图服务；ComfyUI 需要选择实际 checkpoint，云端需要独立 Key 和模型",
            )
        if len(self.jobs) >= 2:
            raise ImageGenerationError("busy", "生图任务较多，请稍后再试")
        task = asyncio.current_task()
        self.jobs.add(task)

        async def invoke(provider, value, selected):
            return await provider.generate(value, selected)

        try:
            return await self.router.execute(prompt, settings, purpose, invoke, only=only)
        finally:
            self.jobs.discard(task)

    async def close(self):
        self.closed = True
        jobs = list(self.jobs)
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
