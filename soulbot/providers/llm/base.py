from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class ProviderError(Exception):
    def __init__(self, kind: str, message: str, *, retryable=False, uncertain=False, usage=None):
        super().__init__(message)
        self.kind, self.retryable, self.uncertain = kind, retryable, uncertain
        self.usage = usage


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None = None


@dataclass
class Completion:
    text: str
    usage: Usage | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    estimated: bool = False
    cost_cny: float | None = None
    finish_reason: str | None = None


DeltaHandler = Callable[[str], Awaitable[None]]


def text_content(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def has_images(messages):
    return any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )


def estimate_tokens(messages: list[dict]) -> int:
    # Text uses a conservative byte bound. Image reservation is an estimate, not a tokenizer.
    # Native vision tokens depend on the provider; measured usage replaces this reservation.
    import json

    total = 16
    for message in messages:
        compact = dict(message)
        if isinstance(compact.get("content"), list):
            parts = []
            for part in compact["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    parts.append({"type": "image_url", "image_url": "[transient image]"})
                    total += 2048
                else:
                    parts.append(part)
            compact["content"] = parts
        total += 12 + len(json.dumps(compact, ensure_ascii=False).encode("utf-8"))
    return total


def trim_context(messages: list[dict], maximum: int) -> list[dict]:
    result = list(messages)
    while len(result) > 2 and estimate_tokens(result) > maximum:
        users = [i for i, item in enumerate(result) if item.get("role") == "user"]
        # Drop complete older turns. Keep the current user + tool-call/result chain intact.
        if len(users) < 2 or users[0] != 1:
            break
        del result[1 : users[1]]
    if estimate_tokens(result) > maximum:
        raise ProviderError("input_limit", "输入内容过长，请缩短消息或减少人格 Prompt")
    return result
