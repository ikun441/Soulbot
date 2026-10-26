"""Bounded speech synthesis with persistent quotas and conservative accounting."""

import asyncio
import time
import uuid

from soulbot.providers.tts.base import SpeechError
from soulbot.providers.tts.edge import EdgeTTSProvider
from soulbot.providers.tts.openai_compatible import OpenAICompatibleTTSProvider


class TTSService:
    def __init__(self, config, secrets, registry, budget, db, diagnostics):
        self.config, self.registry, self.budget, self.db = config, registry, budget, db
        registry.register("tts", "edge", EdgeTTSProvider(), default=True)
        registry.register("tts", "openai_compatible", OpenAICompatibleTTSProvider(secrets))
        self.secrets = secrets
        self.diagnostics = diagnostics
        self.jobs = set()
        self.closed = False

    def test_inputs(self):
        settings = self.config.current.tts.model_copy(deep=True)
        secret = (
            self.secrets.get("tts_api_key") if settings.provider == "openai_compatible" else None
        )
        return settings, secret

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
        settings = self.config.current.tts
        return settings.provider == "edge" or bool(
            self.secrets.get("tts_api_key")
            and settings.compatible.model
            and settings.compatible.voice
        )

    def reserve(self, settings, text, purpose):
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
            used, unknown = self.budget.cost_totals(db, day)
            if used >= settings.behavior.daily_cost_cny or (
                cost is not None and used + cost > settings.behavior.daily_cost_cny
            ):
                raise SpeechError("budget", "今日费用预算不足，已停止语音生成")
            if purpose == "proactive" and (cost is None or unknown):
                raise SpeechError("unknown_cost", "存在未知费用，已暂停主动语音生成")
            key = uuid.uuid4().hex
            model = policy.edge.voice if policy.provider == "edge" else policy.compatible.model
            db.execute(
                "INSERT INTO tts_usage VALUES (?,?,?,?,?,'pending',?,?,?,NULL)",
                (key, day, policy.provider, model, purpose, len(text), cost, now),
            )
        return key

    def finish(self, key, status, kind=None):
        with self.db.connect() as db:
            db.execute("UPDATE tts_usage SET status=?,error_kind=? WHERE id=?", (status, kind, key))
            if status == "rejected":
                db.execute("UPDATE tts_usage SET cost_cny=0 WHERE id=?", (key,))

    async def synthesize(self, text, purpose="explicit"):
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
        key = None
        secret = self.secrets.get("tts_api_key") if policy.provider == "openai_compatible" else None
        try:
            # Synchronous short transaction avoids cancellation leaking a reservation.
            key = self.reserve(settings, text, purpose)
            provider = self.registry.resolve("tts", policy.provider)
            async with asyncio.timeout(policy.timeout_seconds):
                result = await provider.synthesize(text, policy)
            if result.duration > policy.max_duration_seconds:
                raise SpeechError("duration", "返回语音超过配置的时长上限", uncertain=True)
            self.record_test(policy, secret, ok=True)
            self.finish(key, "completed")
            return result
        except asyncio.CancelledError:
            if key:
                self.record_test(policy, secret, ok=False, error_kind="cancelled")
                self.finish(key, "uncertain", "cancelled")
            raise
        except TimeoutError:
            if key:
                self.record_test(policy, secret, ok=False, error_kind="timeout")
                self.finish(key, "uncertain", "timeout")
            raise SpeechError(
                "timeout", "语音生成超时；服务商可能已产生费用", uncertain=True
            ) from None
        except SpeechError as error:
            if key:
                self.record_test(policy, secret, ok=False, error_kind=error.kind)
                self.finish(key, "uncertain" if error.uncertain else "rejected", error.kind)
            raise
        except Exception:
            if key:
                self.record_test(policy, secret, ok=False, error_kind="internal")
                self.finish(key, "uncertain", "internal")
            raise SpeechError(
                "internal", "语音生成未完成，请检查服务配置", uncertain=True
            ) from None
        finally:
            self.jobs.discard(task)

    async def close(self):
        self.closed = True
        jobs = list(self.jobs)
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
