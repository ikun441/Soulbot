"""Bounded speech synthesis with persistent quotas and conservative accounting."""

import asyncio
import time
import uuid

from soulbot.providers.tts.base import SpeechError
from soulbot.providers.tts.edge import EdgeTTSProvider
from soulbot.providers.tts.openai_compatible import OpenAICompatibleTTSProvider
from soulbot.services.media_routing import MediaRouter


class TTSService:
    def __init__(self, config, secrets, registry, budget, db, diagnostics):
        self.config, self.registry, self.budget, self.db = config, registry, budget, db
        registry.register("tts", "edge", EdgeTTSProvider(), default=True)
        registry.register("tts", "openai_compatible", OpenAICompatibleTTSProvider(secrets))
        self.secrets = secrets
        self.diagnostics = diagnostics
        self.router = MediaRouter(self, "tts", SpeechError)
        for name in ("fallback_1", "fallback_2"):
            registry.register("tts", name, OpenAICompatibleTTSProvider(secrets, "tts_" + name))
        self.jobs = set()
        self.closed = False

    def test_inputs(self):
        settings = self.config.current.tts.model_copy(deep=True)
        return settings, self.router.credentials(settings)

    def test_status(self):
        settings, secret = self.test_inputs()
        return self.diagnostics.status(
            "tts",
            settings,
            secret,
            enabled=settings.enabled,
            configured=self.configured(),
        )

    def record_test(self, settings, secret, *, ok, error_kind=None):
        self.diagnostics.record("tts", settings, secret, ok=ok, error_kind=error_kind)

    def configured(self):
        return self.router.configured()

    def reserve(self, settings, text, purpose, *, route=None):
        policy, now = settings.tts, time.time()
        day = self.budget.day(settings)
        price = 0 if policy.provider == "edge" else policy.compatible.price_cny_per_1000_chars
        cost = None if price is None else len(text) * price / 1000
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COUNT(*) AS calls,COALESCE(SUM(chars),0) AS chars FROM tts_usage WHERE day=?",
                (day,),
            ).fetchone()
            recent = db.execute(
                "SELECT COUNT(*) FROM tts_usage WHERE created_at>?", (now - 60,)
            ).fetchone()[0]
            if (
                recent >= policy.max_calls_per_minute
                or row["calls"] >= policy.max_calls_per_day
                or row["chars"] + len(text) > policy.max_chars_per_day
            ):
                raise SpeechError("quota", "语音调用频率或今日字符额度已达上限")
            key = uuid.uuid4().hex
            model = policy.edge.voice if policy.provider == "edge" else policy.compatible.model
            db.execute(
                "INSERT INTO tts_usage VALUES (?,?,?,?,?,'pending',?,?,?,NULL)",
                (key, day, policy.provider, model, purpose, len(text), cost, now),
            )
            self.router.reserve_details(db, key, route)
            self.budget.record_warnings(db, settings, day=day)
        return key

    def finish(self, key, status, kind=None):
        with self.db.connect() as db:
            db.execute("UPDATE tts_usage SET status=?,error_kind=? WHERE id=?", (status, kind, key))
            if status == "rejected":
                db.execute("UPDATE tts_usage SET cost_cny=0 WHERE id=?", (key,))

    async def synthesize(self, text, purpose="explicit", *, only=None):
        settings = self.config.current.model_copy(deep=True)
        policy = settings.tts
        text = text.strip()
        if self.closed or not policy.enabled:
            raise SpeechError("disabled", "语音功能未启用")
        if not text or len(text) > policy.max_text_chars:
            raise SpeechError("input", f"语音文字需要 1–{policy.max_text_chars} 个字符")
        if not self.configured():
            raise SpeechError("configuration", "请填写独立语音密钥、模型和音色")
        if len(self.jobs) >= 2:
            raise SpeechError("busy", "语音任务较多，请稍后再试")
        task = asyncio.current_task()
        self.jobs.add(task)

        async def invoke(provider, value, selected):
            result = await provider.synthesize(value, selected)
            if result.duration > policy.max_duration_seconds:
                raise SpeechError("duration", "返回语音超过配置的时长上限", uncertain=True)
            return result

        try:
            return await self.router.execute(text, settings, purpose, invoke, only=only)
        finally:
            self.jobs.discard(task)

    async def close(self):
        self.closed = True
        jobs = list(self.jobs)
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
