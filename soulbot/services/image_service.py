import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from soulbot.providers.image.base import ImageGenerationError
from soulbot.providers.image.comfyui import ComfyUIProvider
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
        registry.register("image_generation", "comfyui", ComfyUIProvider())
        self.jobs, self.closed = set(), False
        self.last_test = None

    def _provider_fingerprint(self, settings):
        policy = settings.image_generation
        value = {
            "provider": policy.provider,
            "comfy": policy.comfy.model_dump() if policy.provider == "comfyui" else None,
            "base_url": policy.base_url,
            "model": policy.model,
            "size": policy.size,
            "quality": policy.quality,
            "response_format": policy.response_format,
            "output_format": policy.output_format,
            # Hash the secret together with the configuration; never return or persist it.
            "key": self.secrets.get("image_api_key"),
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def diagnostics(self):
        settings = self.config.current
        policy = settings.image_generation
        configured = self.configured()
        if not policy.enabled:
            self.last_test = None
            state = "disabled"
        elif not configured:
            self.last_test = None
            state = "unconfigured"
        elif self.last_test is None or self.last_test["fingerprint"] != self._provider_fingerprint(
            settings
        ):
            self.last_test = None
            state = "not_tested"
        else:
            state = "available" if self.last_test["ok"] else "failed"
        current_test = self.last_test if state in {"available", "failed"} else None
        return {
            "state": state,
            "last_test_at": current_test["tested_at"] if current_test else None,
            "last_error_kind": current_test["error_kind"] if current_test else None,
        }

    def _record_test(self, fingerprint, *, ok, error_kind=None):
        self.last_test = {
            "fingerprint": fingerprint,
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "ok": ok,
            "error_kind": error_kind,
        }

    def configured(self):
        if self.config.current.image_generation.provider == "comfyui":
            return bool(self.config.current.image_generation.comfy.checkpoint)
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
            self.budget.record_warnings(db, settings, day=day)
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
        key = None
        fingerprint = None
        try:
            # Reserve synchronously so cancellation cannot lose a billable attempt.
            key = self.reserve(settings, purpose)
            fingerprint = self._provider_fingerprint(settings)
            async with asyncio.timeout(policy.timeout_seconds):
                result = await self.registry.resolve("image_generation", policy.provider).generate(
                    prompt, policy
                )
            self._record_test(fingerprint, ok=True)
            self.finish(key, "completed")
            return result
        except asyncio.CancelledError:
            if key:
                self._record_test(fingerprint, ok=False, error_kind="cancelled")
                self.finish(key, "uncertain", "cancelled")
            raise
        except TimeoutError:
            if key:
                self._record_test(fingerprint, ok=False, error_kind="timeout")
                self.finish(key, "uncertain", "timeout")
            raise ImageGenerationError("timeout", "生图超时，服务商可能已计费", True) from None
        except ImageGenerationError as error:
            if key:
                self._record_test(fingerprint, ok=False, error_kind=error.kind)
                self.finish(key, "uncertain" if error.uncertain else "rejected", error.kind)
            raise
        except Exception:
            if key:
                self._record_test(fingerprint, ok=False, error_kind="internal")
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
