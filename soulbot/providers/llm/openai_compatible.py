"""The documented Chat Completions wire protocol; no SDK or model list assumptions."""

import asyncio
import json
from collections.abc import Awaitable, Callable

import httpx

from soulbot.providers.llm.base import Completion, ProviderError, Usage

MAX_BODY = 2 * 1024 * 1024


def parse_usage(value):
    if value is None:
        return None
    try:
        numbers = [value["prompt_tokens"], value["completion_tokens"]]
        if any(type(n) is not int or n < 0 or n > 100000000 for n in numbers):
            raise ValueError
        reasoning = (value.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if reasoning is not None and (
            type(reasoning) is not int or reasoning < 0 or reasoning > numbers[1]
        ):
            raise ValueError
        return Usage(*numbers, reasoning)
    except (TypeError, KeyError, ValueError, AttributeError):
        raise ProviderError(
            "invalid_response", "模型返回了无效的 Token 用量", uncertain=True
        ) from None


def parse_calls(calls):
    if not calls:
        return []
    try:
        if not isinstance(calls, list) or len(calls) > 5:
            raise ValueError
        checked = []
        for item in calls:
            function = item["function"]
            if (
                item.get("type", "function") != "function"
                or not isinstance(item["id"], str)
                or len(item["id"]) > 200
            ):
                raise ValueError
            name, arguments = function["name"], function["arguments"]
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 100
                or not isinstance(arguments, str)
                or len(arguments) > 16384
            ):
                raise ValueError
            if not isinstance(json.loads(arguments), dict):
                raise ValueError
            checked.append(
                {
                    "id": item["id"],
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
        return checked
    except (TypeError, KeyError, ValueError):
        raise ProviderError(
            "invalid_tool_call", "模型工具参数无效，未执行任何工具", uncertain=True
        ) from None


class OpenAICompatibleProvider:
    def __init__(self, settings, api_key, transport=None):
        self.settings, self.api_key, self.transport = settings, api_key, transport
        self.capabilities = frozenset(
            {"llm"}
            | ({"streaming"} if settings.streaming else set())
            | ({"tool_calling"} if settings.supports_tools else set())
        )

    def client(self):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                self.settings.timeout_seconds, connect=min(10, self.settings.timeout_seconds)
            ),
            headers={"Authorization": "Bearer " + self.api_key},
            follow_redirects=False,
            transport=self.transport,
        )

    @staticmethod
    def error(status):
        if status in {401, 403}:
            return ProviderError("authentication", "API Key 无效或没有访问权限")
        if status == 404:
            return ProviderError("model_not_found", "模型或接口不存在，请核对模型 ID 和 Base URL")
        if status == 429:
            return ProviderError("rate_limit", "服务商限流或额度不足，请稍后重试", retryable=True)
        if status >= 500:
            return ProviderError(
                "unavailable", "模型服务暂时不可用", retryable=True, uncertain=True
            )
        return ProviderError("bad_request", "服务商拒绝了请求，请检查模型参数和上下文上限")

    async def models(self):
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self.client() as client:
                    async with client.stream("GET", self.settings.base_url + "/models") as response:
                        if response.status_code != 200:
                            raise self.error(response.status_code)
                        body = await self.read_body(response)
            value = json.loads(body)
            data = value["data"]
            if not isinstance(data, list):
                raise ValueError
            return [
                item["id"]
                for item in data[:500]
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and len(item["id"]) <= 200
            ]
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderError("timeout", "模型服务连接超时", retryable=True) from None
        except httpx.HTTPError:
            raise ProviderError(
                "network", "无法连接模型服务，请检查地址与网络", retryable=True
            ) from None
        except (ValueError, KeyError, TypeError):
            raise ProviderError("invalid_response", "模型列表响应格式不兼容") from None

    async def health_check(self):
        return self.settings.model in await self.models()

    async def close(self):
        # Each request owns and closes its HTTP connection pool.
        return None

    @staticmethod
    async def read_body(response):
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise ProviderError("invalid_response", "服务商响应超过大小限制", uncertain=True)
        return bytes(body)

    async def complete(
        self,
        messages,
        maximum,
        tools=None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ):
        streaming = self.settings.streaming
        if tools and not self.settings.supports_tools:
            raise ProviderError("unsupported_capability", "此 Provider 未声明工具调用能力")
        payload = {
            "model": self.settings.model,
            "messages": messages,
            self.settings.max_output_field: maximum,
            "stream": streaming,
        }
        if streaming and self.settings.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self.client() as client:
                    async with client.stream(
                        "POST", self.settings.base_url + "/chat/completions", json=payload
                    ) as response:
                        if response.status_code != 200:
                            raise self.error(response.status_code)
                        if streaming:
                            return await self.stream(response, on_delta)
                        value = json.loads(await self.read_body(response))
                        choice = value["choices"][0]
                        message = choice["message"]
                        text = message.get("content") or ""
                        if not isinstance(text, str):
                            raise ValueError
                        calls = parse_calls(message.get("tool_calls"))
                        if not text and not calls and not message.get("refusal"):
                            raise ValueError
                        if on_delta and text:
                            await on_delta(text)
                        return Completion(
                            text,
                            parse_usage(value.get("usage")),
                            calls,
                            model=self.settings.model,
                            finish_reason=choice.get("finish_reason"),
                        )
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderError(
                "timeout", "模型响应超时，本次消耗将保守记账", retryable=True, uncertain=True
            ) from None
        except httpx.HTTPError:
            raise ProviderError(
                "network", "模型连接中断，本次消耗将保守记账", retryable=True, uncertain=True
            ) from None
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise ProviderError("invalid_response", "模型响应格式不兼容", uncertain=True) from None

    async def stream(self, response, on_delta):
        text, calls, usage, size, finished = [], {}, None, 0, False
        data_lines = []
        async for line in response.aiter_lines():
            size += len(line.encode("utf-8"))
            if size > MAX_BODY:
                raise ProviderError("invalid_response", "流式响应过大", uncertain=True)
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue
            if line or not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines.clear()
            if data == "[DONE]":
                finished = True
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise ProviderError("unavailable", "服务商在生成过程中返回错误", uncertain=True)
            if chunk.get("usage") is not None:
                usage = parse_usage(chunk["usage"])
            for choice in chunk.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                content = delta.get("content") or ""
                if not isinstance(content, str):
                    raise ValueError
                if content:
                    text.append(content)
                    if on_delta:
                        await on_delta(content)
                for item in delta.get("tool_calls") or []:
                    index = item["index"]
                    if type(index) is not int or not 0 <= index < 5:
                        raise ValueError
                    target = calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if item.get("id"):
                        target["id"] += item["id"]
                    for key in ("name", "arguments"):
                        target["function"][key] += (item.get("function") or {}).get(key) or ""
        if not finished:
            raise ProviderError(
                "stream_interrupted", "流式响应未正常结束，用量已保守保留", uncertain=True
            )
        return Completion(
            "".join(text), usage, parse_calls(list(calls.values())), model=self.settings.model
        )
