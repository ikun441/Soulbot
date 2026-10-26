import asyncio
import time
import uuid

from soulbot.providers.asr.base import ASRError
from soulbot.providers.asr.local import LocalASRProvider
from soulbot.providers.asr.openai_compatible import OpenAICompatibleASRProvider
from soulbot.services.audio_service import inspect_audio


class ASRService:
    def __init__(self, root, config, secrets, registry, budget, db):
        self.config, self.secrets, self.registry, self.budget, self.db = (
            config,
            secrets,
            registry,
            budget,
            db,
        )
        self.local = LocalASRProvider(root)
        registry.register("asr", "local", self.local, default=True)
        registry.register("asr", "openai_compatible", OpenAICompatibleASRProvider(secrets))
        self.jobs, self.closed = set(), False

    def configured(self):
        settings = self.config.current.asr
        return (
            self.local.ready()
            if settings.provider == "local"
            else bool(settings.model.strip() and self.secrets.get("asr_api_key"))
        )

    def reserve(self, settings, seconds, purpose):
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
            used, _ = self.budget.cost_totals(db, day)
            if used >= settings.behavior.daily_cost_cny or (
                cost is not None and used + cost > settings.behavior.daily_cost_cny
            ):
                raise ASRError("budget", "今日共享费用预算不足，已停止识别")
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
        return key

    def finish(self, key, status, kind=None):
        with self.db.connect() as db:
            db.execute("UPDATE asr_usage SET status=?,error_kind=? WHERE id=?", (status, kind, key))
            if status == "rejected":
                db.execute("UPDATE asr_usage SET cost_cny=0 WHERE id=?", (key,))

    async def transcribe(self, content, purpose="web_test"):
        settings = self.config.current.model_copy(deep=True)
        policy = settings.asr
        if self.closed or not policy.enabled:
            raise ASRError("disabled", "语音识别尚未启用")
        if not self.configured():
            raise ASRError("configuration", "请先准备本地模型，或填写独立 ASR Key 和模型")
        if len(self.jobs) >= (1 if policy.provider == "local" else 2):
            raise ASRError("busy", "识别任务较多，请稍后再试")
        task = asyncio.current_task()
        self.jobs.add(task)
        key = None
        try:
            audio = await asyncio.to_thread(inspect_audio, content)
            if audio.duration > policy.max_duration_seconds:
                raise ASRError("duration", "录音超过识别时长上限，请截短后重试")
            # Brief atomic transaction: cancellation cannot lose a billable reservation.
            key = self.reserve(settings, audio.duration, purpose)
            async with asyncio.timeout(policy.timeout_seconds):
                result = await self.registry.resolve("asr", policy.provider).transcribe(
                    audio, policy
                )
            self.finish(key, "completed")
            return result
        except asyncio.CancelledError:
            if key:
                self.finish(key, "uncertain", "cancelled")
            raise
        except TimeoutError:
            if key:
                self.finish(key, "uncertain", "timeout")
            raise ASRError("timeout", "识别超时，云端请求可能已计费；没有自动重试", True) from None
        except ASRError as error:
            if key:
                self.finish(key, "uncertain" if error.uncertain else "rejected", error.kind)
            raise
        except Exception:
            if key:
                self.finish(key, "uncertain", "internal")
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
