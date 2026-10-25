from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class ProviderError(Exception):
    def __init__(self, kind: str, message: str, *, retryable=False, uncertain=False):
        super().__init__(message)
        self.kind, self.retryable, self.uncertain = kind, retryable, uncertain


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


def estimate_tokens(messages: list[dict]) -> int:
    # A conservative UTF-8 byte bound, not a tokenizer or provider usage measurement.
    import json

    return 16 + sum(12 + len(json.dumps(m, ensure_ascii=False).encode("utf-8")) for m in messages)


def trim_context(messages: list[dict], maximum: int) -> list[dict]:
    result = list(messages)
    while len(result) > 2 and estimate_tokens(result) > maximum:
        result.pop(1)
        if len(result) > 2 and result[1].get("role") == "assistant":
            result.pop(1)
    if estimate_tokens(result) > maximum:
        raise ProviderError("input_limit", "输入内容过长，请缩短消息或减少人格 Prompt")
    return result
