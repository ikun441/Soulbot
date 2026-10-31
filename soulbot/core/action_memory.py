"""Fresh memory preflight for robot operations; identities come only from server context."""

import asyncio
import json
import time
from contextvars import ContextVar
from functools import wraps

from soulbot.core.events import ChatEvent

current_event = ContextVar("soulbot_memory_event", default=None)


def bind_chat_event(method):
    @wraps(method)
    async def wrapped(self, batch, *args, **kwargs):
        token = current_event.set(batch[-1][0] if batch else None)
        try:
            return await method(self, batch, *args, **kwargs)
        finally:
            current_event.reset(token)

    return wrapped


class ActionMemory:
    def __init__(self, services):
        self.s = services

    def default_event(self, query):
        owner = self.s.config.current.personality.owner_qq
        bots = self.s.qq_transport.connected() if self.s.qq_transport else []
        if owner and len(bots) == 1:
            return ChatEvent(bots[0], owner, "system-action", query, directed=True)
        return None

    def retrieve(self, event, query, operation):
        cfg = self.s.config.current
        policy = cfg.action_memory
        if not policy.enabled:
            return {"local": [], "global": [], "scope": None, "enabled": False}
        event = event or current_event.get() or self.default_event(query)
        local = []
        if event is not None and policy.local and self.s.memory.permitted(event):
            from dataclasses import replace

            local = self.s.memory.retrieve(replace(event, text=query[:1000]))
            local = [
                dict(item, scope=event.scope, visibility="current_conversation") for item in local
            ]
        global_items = self.s.memory.action_context(query[:1000]) if policy.global_memory else []
        record = {
            "operation": operation[:80],
            "scope": event.scope if event else None,
            "local_count": len(local),
            "global_count": len(global_items),
            "created_at": time.time(),
        }
        self.s.db.set("action_memory_last", json.dumps(record, ensure_ascii=False))
        self.s.db.audit(
            "action_memory_retrieved",
            json.dumps({k: v for k, v in record.items() if k != "scope"}, ensure_ascii=False),
        )
        return {"local": local, "global": global_items, "scope": record["scope"], "enabled": True}

    async def collect(self, event=None, query="", operation="action"):
        return await asyncio.to_thread(self.retrieve, event, str(query or "")[:1000], operation)

    async def tool_preflight(self, name, arguments, context):
        query = getattr(context.event, "text", "") or arguments.model_dump_json()
        context.memory_references = await self.collect(context.event, query, "tool:" + name)

    async def send_preflight(self, event, payload, permission):
        return await self.collect(
            event, payload if isinstance(payload, str) else event.text, permission
        )

    def status(self):
        return {
            "policy": self.s.config.current.action_memory.model_dump(),
            "memory_enabled": self.s.config.current.memory.enabled,
            "last": json.loads(self.s.db.get("action_memory_last") or "{}"),
        }
