import asyncio
import random

from soulbot.providers.llm.base import ProviderError, estimate_tokens, trim_context
from soulbot.providers.llm.openai_compatible import OpenAICompatibleProvider


class LLMService:
    def __init__(self, config, secrets, registry, budget, audit):
        self.config, self.secrets, self.registry, self.budget, self.audit = (
            config,
            secrets,
            registry,
            budget,
            audit,
        )
        self.transport = None
        self.limiter = asyncio.Semaphore(4)

    def configured(self):
        settings = self.config.current
        return bool(
            (settings.llm.enabled and settings.llm.model and self.secrets.get("llm_api_key"))
            or any(
                e.enabled and e.model and self.secrets.get("llm_" + name)
                for name, e in settings.llm.fallbacks.items()
            )
        )

    def chain(self, settings, required=(), allow_unselected=False):
        endpoints = [("primary", settings.llm, "llm_api_key")]
        endpoints += [
            (name, endpoint, "llm_" + name) for name, endpoint in settings.llm.fallbacks.items()
        ]
        chain = []
        self.registry.providers = {
            key: value for key, value in self.registry.providers.items() if key[0] != "llm"
        }
        self.registry.defaults.pop("llm", None)
        for name, endpoint, slot in endpoints:
            key = self.secrets.get(slot)
            if not endpoint.enabled or (not endpoint.model and not allow_unselected) or not key:
                continue
            provider = OpenAICompatibleProvider(endpoint, key, self.transport)
            if not set(required) <= provider.capabilities:
                continue
            self.registry.providers.pop(("llm", name), None)
            self.registry.register("llm", name, provider, default=name == "primary")
            chain.append((name, endpoint, self.registry.resolve("llm", name)))
        if not chain:
            raise ProviderError(
                "not_configured",
                "请先在 AI 模型页面配置并启用模型和 API Key（所需能力也必须已声明）",
            )
        return chain

    async def models(self, name="primary"):
        for selected, endpoint, provider in self.chain(
            self.config.current.model_copy(deep=True), allow_unselected=True
        ):
            if selected == name:
                values = await provider.models()
                return {
                    "models": values,
                    "selected_model_available": endpoint.model in values,
                    "provider": name,
                }
        raise ProviderError("not_configured", "该 Provider 尚未配置")

    async def complete(
        self,
        messages,
        purpose="explicit",
        tools=None,
        on_delta=None,
        output_limit=None,
        task_state=None,
    ):
        settings = self.config.current.model_copy(deep=True)
        required = ["tool_calling"] if tools else []
        chain = self.chain(settings, required)
        total, last_error, emitted = (task_state or {}).get("tokens", 0), None, False

        async def delta(text):
            nonlocal emitted
            if on_delta:
                emitted = True
                await on_delta(text)

        async with self.limiter:
            for name, endpoint, provider in chain:
                maximum = min(
                    output_limit or settings.limits.max_output_per_request,
                    settings.limits.max_output_per_request,
                    endpoint.context_window // 2,
                )
                prepared = trim_context(
                    messages,
                    min(
                        settings.limits.max_input_per_request,
                        endpoint.context_window - maximum - 128,
                    )
                    - (estimate_tokens([{"tools": tools}]) if tools else 0),
                )
                amount = estimate_tokens(prepared) + (
                    estimate_tokens([{"tools": tools}]) if tools else 0
                )
                for attempt in range(endpoint.retries + 1):
                    if total + amount + maximum > settings.limits.max_per_agent_task:
                        raise ProviderError(
                            "task_budget", "本次任务的 Token 预算已用尽，已停止重试与故障切换"
                        )
                    key = await asyncio.to_thread(
                        self.budget.reserve, settings, endpoint, name, purpose, amount, maximum
                    )
                    total += amount + maximum
                    if task_state is not None:
                        task_state["tokens"] = total
                    await asyncio.to_thread(self.audit, "llm_started", name)
                    try:
                        result = await provider.complete(
                            prepared, maximum, tools, delta if on_delta else None
                        )
                        await asyncio.to_thread(self.budget.settle, key, endpoint, result.usage)
                        result.provider, result.model = name, endpoint.model
                        result.estimated = result.usage is None
                        result.cost_cny = self.budget.price(
                            endpoint,
                            result.usage.input_tokens if result.usage else amount,
                            result.usage.output_tokens if result.usage else maximum,
                        )
                        await asyncio.to_thread(self.audit, "llm_completed", name)
                        return result
                    except asyncio.CancelledError:
                        await asyncio.shield(
                            asyncio.to_thread(
                                self.budget.failed,
                                key,
                                ProviderError("cancelled", "请求已取消", uncertain=True),
                            )
                        )
                        raise
                    except ProviderError as error:
                        await asyncio.to_thread(self.budget.failed, key, error)
                        await asyncio.to_thread(self.audit, "llm_failed", name + ":" + error.kind)
                        last_error = error
                        if emitted:
                            raise
                        if not error.retryable or attempt == endpoint.retries:
                            break
                        await asyncio.to_thread(self.audit, "llm_retry", name)
                        await asyncio.sleep(min(0.25 * (2**attempt) + random.random() * 0.1, 1))
                await asyncio.to_thread(self.audit, "llm_fallback", name)
        raise last_error or ProviderError("unavailable", "没有可用的模型服务")
