"""Capability-bound media routing, with independent reservations for each actual attempt."""

import asyncio
from dataclasses import replace
from uuid import uuid4

from soulbot.services.config_service import ASREndpoint, ImageGenerationEndpoint, SpeechEndpoint

FALLBACK_ERRORS = {
    "authentication",
    "rate_limit",
    "unavailable",
    "network",
    "timeout",
    "format",
    "not_ready",
    "not_configured",
    "configuration",
    "voice",
    "busy",
}
AUTO_PURPOSES = {"proactive", "proactive_draft", "exploration", "qzone_autonomous", "autonomous"}


class MediaRouter:
    def __init__(self, service, capability, error_type, diagnostics=None):
        self.s, self.capability, self.error_type = service, capability, error_type
        self.endpoint_type = {
            "tts": SpeechEndpoint,
            "asr": ASREndpoint,
            "image_generation": ImageGenerationEndpoint,
        }[capability]
        self.diagnostics = diagnostics or service.diagnostics
        self.label = "生图" if capability == "image_generation" else "语音"

    @property
    def policy(self):
        return getattr(self.s.config.current, self.capability)

    def endpoints(self, policy, *, all_saved=False):
        primary = self.endpoint_type.model_construct(
            **{key: getattr(policy, key) for key in self.endpoint_type.model_fields}
        )
        return [
            ("primary", primary),
            *[
                (name, policy.fallbacks[name])
                for name in ("fallback_1", "fallback_2")
                if name in policy.fallbacks and (all_saved or policy.fallbacks[name].enabled)
            ],
        ]

    def secret_name(self, name):
        return ("image" if self.capability == "image_generation" else self.capability) + (
            "_api_key" if name == "primary" else "_" + name
        )

    def credentials(self, policy):
        return {
            name: self.s.secrets.get(self.secret_name(name))
            if endpoint.provider == "openai_compatible"
            else None
            for name, endpoint in self.endpoints(policy)
        }

    def ready(self, name, endpoint):
        if endpoint.provider == "edge":
            return True
        if endpoint.provider == "comfyui":
            return bool(endpoint.comfy.checkpoint)
        if endpoint.provider == "local":
            return self.s.local.ready()
        fields = endpoint.compatible if self.capability == "tts" else endpoint
        return bool(
            self.s.secrets.get(self.secret_name(name))
            and fields.model.strip()
            and (fields.voice.strip() if self.capability == "tts" else True)
        )

    def configured(self):
        return any(self.ready(name, endpoint) for name, endpoint in self.endpoints(self.policy))

    def provider(self, name, endpoint):
        selected = (
            name
            if name != "primary" and endpoint.provider == "openai_compatible"
            else endpoint.provider
        )
        provider = self.s.registry.resolve(self.capability, selected)
        if self.capability not in provider.capabilities:
            raise self.error_type("configuration", "所选 Provider 不支持当前能力")
        return provider

    def status(self):
        policy = self.policy
        return [
            {
                "id": name,
                "provider": endpoint.provider,
                **self.diagnostics.status(
                    self.capability + ":" + name,
                    endpoint,
                    self.s.secrets.get(self.secret_name(name))
                    if endpoint.provider == "openai_compatible"
                    else None,
                    enabled=policy.enabled and endpoint.enabled,
                    configured=self.ready(name, endpoint),
                ),
            }
            for name, endpoint in self.endpoints(policy, all_saved=True)
        ]

    def guard(self, policy, credentials, purpose):
        cfg = self.s.config.current
        if self.s.closed or self.policy != policy or credentials != self.credentials(self.policy):
            raise self.error_type(
                "configuration", f"{self.label}配置、密钥或开关已变化，已停止继续尝试"
            )
        if purpose in AUTO_PURPOSES and cfg.automation.paused:
            raise self.error_type("permission", "自主行为已暂停，已停止继续尝试")
        if (
            self.capability in {"tts", "image_generation"}
            and purpose != "web_test"
            and (
                getattr(
                    cfg.tools,
                    "generate_image"
                    if self.capability == "image_generation"
                    else "synthesize_speech",
                )
                == "deny"
                or purpose in AUTO_PURPOSES
                and getattr(
                    cfg.tools,
                    "generate_image"
                    if self.capability == "image_generation"
                    else "synthesize_speech",
                )
                != "auto"
            )
        ):
            raise self.error_type("permission", f"{self.label}工具权限已关闭")
        if (
            self.capability == "asr"
            and not policy.allow_cloud_fallback
            and any(
                e.enabled and e.provider == "openai_compatible" for e in policy.fallbacks.values()
            )
        ):
            raise self.error_type("permission", "未授权向备用云端服务上传录音")

    async def execute(self, value, settings, purpose, invoke, *, only=None):
        policy = getattr(settings, self.capability)
        credentials = self.credentials(policy)
        routes = self.endpoints(policy)
        if only is not None:
            routes = [(name, endpoint) for name, endpoint in routes if name == only]
            if not routes:
                raise self.error_type("configuration", "所选备用服务未启用或不存在")
        deadline = asyncio.get_running_loop().time() + policy.total_timeout_seconds
        request_id = uuid4().hex
        last_error = self.error_type("configuration", "请先配置本地模型或独立服务密钥与模型")
        for index, (name, endpoint) in enumerate(routes):
            self.guard(policy, credentials, purpose)
            if not self.ready(name, endpoint):
                if only is not None:
                    raise last_error
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise self.error_type(
                    "timeout", f"{self.label}任务已达到总超时；已发出的请求可能计费", True
                )
            effective = settings.model_copy(deep=True)
            selected = policy.model_copy(
                update={key: getattr(endpoint, key) for key in self.endpoint_type.model_fields}
            )
            setattr(effective, self.capability, selected)
            key = (
                self.s.reserve(effective, purpose, route=(request_id, name))
                if self.capability == "image_generation"
                else self.s.reserve(effective, value, purpose, route=(request_id, name))
            )
            secret = credentials.get(name)
            try:
                async with asyncio.timeout(min(endpoint.timeout_seconds, remaining)):
                    result = await invoke(self.provider(name, endpoint), value, selected)
                result = replace(
                    result,
                    provider_id=name,
                    **(
                        {"provider": endpoint.provider}
                        if self.capability in {"asr", "image_generation"}
                        else {}
                    ),
                )
            except asyncio.CancelledError:
                self.record(policy, credentials, endpoint, name, secret, key, "cancelled", True)
                raise
            except (self.error_type, TimeoutError) as error:
                if isinstance(error, TimeoutError):
                    error = self.error_type(
                        "timeout", f"{self.label}任务超时；服务商可能已产生费用", True
                    )
                last_error = error
                self.record(
                    policy, credentials, endpoint, name, secret, key, error.kind, error.uncertain
                )
                if (
                    only is not None
                    or error.kind not in FALLBACK_ERRORS
                    or index == len(routes) - 1
                    or self.capability == "image_generation"
                    and error.uncertain
                    and not policy.fallback_on_uncertain
                ):
                    raise error
                self.s.db.audit(self.capability + "_fallback", name + ":" + error.kind)
                continue
            except Exception:
                self.record(policy, credentials, endpoint, name, secret, key, "internal", True)
                raise self.error_type(
                    "internal", f"{self.label}任务未完成，请核对配置与服务状态", True
                ) from None
            self.s.finish(key, "completed")
            self.diagnostics.record(self.capability + ":" + name, endpoint, secret, ok=True)
            self.s.record_test(policy, credentials, ok=True)
            return result
        raise last_error

    def record(self, policy, credentials, endpoint, name, secret, key, kind, uncertain):
        self.s.finish(key, "uncertain" if uncertain else "rejected", kind)
        self.diagnostics.record(
            self.capability + ":" + name, endpoint, secret, ok=False, error_kind=kind
        )
        self.s.record_test(policy, credentials, ok=False, error_kind=kind)

    def reserve_details(self, db, key, route):
        if route:
            request_id, name = route
            db.execute(
                "INSERT INTO media_attempts VALUES (?,?,?,?)",
                (key, request_id, self.capability, name),
            )
