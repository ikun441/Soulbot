import asyncio
import time
import uuid

from soulbot.providers.asr.base import ASRError
from soulbot.providers.asr.local import LocalASRProvider
from soulbot.providers.asr.openai_compatible import OpenAICompatibleASRProvider
from soulbot.services.audio_service import inspect_audio
from soulbot.services.media_routing import MediaRouter


class ASRService:
    def __init__(self, root, config, secrets, registry, budget, db, diagnostics):
        self.config, self.secrets, self.registry, self.budget, self.db = (
            config,
            secrets,
            registry,
            budget,
            db,
        )
        self.diagnostics = diagnostics
        self.local = LocalASRProvider(root)
        registry.register("asr", "local", self.local, default=True)
        registry.register("asr", "openai_compatible", OpenAICompatibleASRProvider(secrets))
        self.router = MediaRouter(self, "asr", ASRError)
        for name in ("fallback_1", "fallback_2"):
            registry.register("asr", name, OpenAICompatibleASRProvider(secrets, "asr_" + name))
        self.jobs, self.closed = set(), False

    def test_inputs(self):
        settings = self.config.current.asr.model_copy(deep=True)
        return settings, self.router.credentials(settings)

    def test_status(self):
        settings, secret = self.test_inputs()
        return self.diagnostics.status(
            "asr",
            settings,
            secret,
            enabled=settings.enabled,
            configured=self.configured(),
        )

    def record_test(self, settings, secret, *, ok, error_kind=None):
        self.diagnostics.record("asr", settings, secret, ok=ok, error_kind=error_kind)

    def configured(self):
        return self.router.configured()

    def reserve(self, settings, seconds, purpose, *, route=None):
        policy, now = settings.asr, time.time()
        price = 0 if policy.provider == "local" else policy.price_cny_per_minute
        cost = None if price is None else seconds * price / 60
        day = self.budget.day(settings)
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(seconds),0) FROM asr_usage WHERE day=?", (day,)
            ).fetchone()
            recent = db.execute(
                "SELECT COUNT(*) FROM asr_usage WHERE created_at>?", (now - 60,)
            ).fetchone()[0]
            if (
                recent >= policy.max_calls_per_minute
                or row[0] >= policy.max_calls_per_day
                or row[1] + seconds > policy.max_seconds_per_day
            ):
                raise ASRError("quota", "识别频率或今日音频秒数额度已达上限")
            key = uuid.uuid4().hex
            db.execute(
                "INSERT INTO asr_usage VALUES (?,?,?,?,?,'pending',?,?,?,NULL)",
                (
                    key,
                    day,
                    policy.provider,
                    "whisper-base" if policy.provider == "local" else policy.model,
                    purpose,
                    seconds,
                    cost,
                    now,
                ),
            )
            self.router.reserve_details(db, key, route)
            self.budget.record_warnings(db, settings, day=day)
        return key

    def finish(self, key, status, kind=None):
        with self.db.connect() as db:
            db.execute("UPDATE asr_usage SET status=?,error_kind=? WHERE id=?", (status, kind, key))
            if status == "rejected":
                db.execute("UPDATE asr_usage SET cost_cny=0 WHERE id=?", (key,))

    async def transcribe(self, content, purpose="web_test", *, only=None):
        settings = self.config.current.model_copy(deep=True)
        policy = settings.asr
        if self.closed or not policy.enabled:
            raise ASRError("disabled", "语音识别尚未启用")
        if not self.configured():
            raise ASRError("configuration", "请先准备本地模型，或填写独立 ASR Key 和模型")
        if len(self.jobs) >= (
            1 if any(e.provider == "local" for _, e in self.router.endpoints(policy)) else 2
        ):
            raise ASRError("busy", "识别任务较多，请稍后再试")
        task = asyncio.current_task()
        self.jobs.add(task)
        try:
            audio = await asyncio.to_thread(inspect_audio, content)
            if audio.duration > policy.max_duration_seconds:
                raise ASRError("duration", "录音超过识别时长上限，请截短后重试")

            async def invoke(provider, value, selected):
                return await provider.transcribe(value, selected)

            return await self.router.execute(
                audio.duration,
                settings,
                purpose,
                lambda provider, seconds, selected: invoke(provider, audio, selected),
                only=only,
            )
        except (ASRError, asyncio.CancelledError):
            raise
        except Exception:
            raise ASRError("invalid", "识别失败，请检查音频格式、模型和网络", True) from None
        finally:
            self.jobs.discard(task)

    async def close(self):
        self.closed = True
        jobs = list(self.jobs)
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await self.local.close()
