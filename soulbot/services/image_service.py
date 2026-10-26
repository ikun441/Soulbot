import asyncio
import time
import uuid

from soulbot.providers.image.base import ImageGenerationError
from soulbot.providers.image.openai_compatible import OpenAICompatibleImageProvider


class ImageGenerationService:
    def __init__(self, config, secrets, registry, budget, db):
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
        self.jobs, self.closed = set(), False

    def configured(self):
        return bool(
            self.config.current.image_generation.model.strip() and self.secrets.get("image_api_key")
        )

    def reserve(self, settings, purpose):
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
            used, unknown = self.budget.cost_totals(db, day)
            if used >= settings.behavior.daily_cost_cny or (
                cost is not None and used + cost > settings.behavior.daily_cost_cny
            ):
                raise ImageGenerationError("budget", "今日共享费用预算不足，已停止生图")
            if purpose == "proactive" and (cost is None or unknown):
                raise ImageGenerationError("unknown_cost", "存在未知费用，已暂停主动生图")
            key = uuid.uuid4().hex
            db.execute(
                "INSERT INTO image_usage VALUES (?,?,?,?,?,'pending',1,?,?,NULL)",
                (key, day, "openai_compatible", policy.model, purpose, cost, now),
            )
        return key

    def finish(self, key, status, kind=None):
        with self.db.connect() as db:
            db.execute(
                "UPDATE image_usage SET status=?,error_kind=? WHERE id=?", (status, kind, key)
            )
            if status == "rejected":
                db.execute("UPDATE image_usage SET cost_cny=0 WHERE id=?", (key,))

    async def generate(self, prompt, purpose="web_test"):
        settings = self.config.current.model_copy(deep=True)
        policy = settings.image_generation
        prompt = prompt.strip()
        if self.closed or not policy.enabled:
            raise ImageGenerationError("disabled", "云端图片生成尚未启用")
        if not prompt or len(prompt) > policy.max_prompt_chars:
            raise ImageGenerationError("input", f"提示词需要 1–{policy.max_prompt_chars} 个字符")
        if not self.configured():
            raise ImageGenerationError("configuration", "请先填写生图服务的独立 Key 和模型 ID")
        if len(self.jobs) >= 2:
            raise ImageGenerationError("busy", "生图任务较多，请稍后再试")
        task = asyncio.current_task()
        self.jobs.add(task)
        key = None
        try:
            # Reserve synchronously so cancellation cannot lose a billable attempt.
            key = self.reserve(settings, purpose)
            async with asyncio.timeout(policy.timeout_seconds):
                result = await self.registry.resolve("image_generation").generate(prompt, policy)
            self.finish(key, "completed")
            return result
        except asyncio.CancelledError:
            if key:
                self.finish(key, "uncertain", "cancelled")
            raise
        except TimeoutError:
            if key:
                self.finish(key, "uncertain", "timeout")
            raise ImageGenerationError("timeout", "生图超时，服务商可能已计费", True) from None
        except ImageGenerationError as error:
            if key:
                self.finish(key, "uncertain" if error.uncertain else "rejected", error.kind)
            raise
        except Exception:
            if key:
                self.finish(key, "uncertain", "internal")
            raise ImageGenerationError("internal", "图片生成未完成，请检查服务配置", True) from None
        finally:
            self.jobs.discard(task)

    async def close(self):
        self.closed = True
        jobs = list(self.jobs)
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
