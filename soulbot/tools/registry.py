import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic import BaseModel

from soulbot.core.permissions import PermissionEngine


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable[..., Awaitable[object]]
    timeout: float = 10
    max_calls: int = 5
    affects_external: bool = False
    with_context: bool = False


@dataclass
class ToolContext:
    """Created by the server for one task, never accepted from LLM arguments."""

    calls: dict[str, int] = field(default_factory=dict)
    total: int = 0
    max_total: int = 5
    scope: str | None = None
    event: object | None = None
    purpose: str = "explicit"
    approved_tools: frozenset[str] = frozenset()
    available_tools: frozenset[str] = frozenset()


class ToolRegistry:
    def __init__(self, permissions: PermissionEngine, audit):
        self.permissions, self.audit = permissions, audit
        self.tools: dict[str, Tool] = {}
        self.argument_guard = None
        self.approval_queue = None

    def register(self, tool: Tool):
        if tool.name in self.tools or tool.timeout <= 0 or tool.max_calls < 1:
            raise ValueError("工具注册无效")
        self.tools[tool.name] = tool

    async def execute(self, name: str, arguments: dict, context: ToolContext):
        if name not in self.tools:
            raise LookupError("工具未注册")
        tool = self.tools[name]
        queued = False
        try:
            self.permissions.require(name, approved=name in context.approved_tools)
        except PermissionError:
            if self.approval_queue and self.permissions.policies.get(name) == "approval":
                queued = True
            else:
                raise
        validated = tool.schema.model_validate(arguments)
        if self.argument_guard:
            self.argument_guard(name, validated, context)
        if context.total >= context.max_total or context.calls.get(name, 0) >= tool.max_calls:
            raise PermissionError("工具调用次数已达上限")
        context.total += 1
        context.calls[name] = context.calls.get(name, 0) + 1
        if queued:
            return self.approval_queue(name, validated, context)
        self.audit("tool_started", name)
        try:
            operation = (
                tool.handler(validated, context) if tool.with_context else tool.handler(validated)
            )
            result = await asyncio.wait_for(operation, tool.timeout)
        except Exception:
            self.audit("tool_failed", name)
            raise
        self.audit("tool_completed", name)
        return result
