"""Model tools have server-owned identities and cannot grant memory consent or publish."""

import asyncio

from pydantic import Field

from soulbot.core.memory import MemoryCreate
from soulbot.services.config_service import Model
from soulbot.services.search_service import SearchRequest
from soulbot.tools.registry import Tool


class MemoryQuery(Model):
    query: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0, le=1000000)


class SaveMemory(Model):
    content: str = Field(min_length=1, max_length=1000)
    importance: int = Field(default=3, ge=1, le=5)


def register_social_tools(s):
    async def search(args, context):
        return await s.search.search(args, context.purpose)

    def event(context):
        if context.event is None or context.event.scope != context.scope:
            raise PermissionError("记忆工具需要系统提供当前 QQ 会话")
        return context.event

    async def retrieve(args, context):
        from dataclasses import replace

        if s.config.current.memory.global_context:
            return await asyncio.to_thread(s.memory.recall, args.query, args.offset, 5)
        current = event(context)
        return {
            "items": await asyncio.to_thread(s.memory.retrieve, replace(current, text=args.query))
        }

    async def save(args, context):
        current = event(context)
        if not s.memory.permitted(current):
            raise PermissionError("本会话记忆已关闭")
        result = await asyncio.to_thread(
            s.memory.create,
            MemoryCreate(
                bot_id=current.bot_id,
                user_id=current.user_id,
                group_id=current.group_id or "",
                content=args.content,
                importance=args.importance,
                source="模型提出的待审核候选",
                long_term_allowed=False,
            ),
            capture_scope=current.scope,
        )
        return {
            "candidate_id": result["item"]["id"],
            "duplicate": result["duplicate"],
            "message": "仅保存待审核候选；未授予长期保存授权，不能视为已生效的长期记忆。",
        }

    for name, description, schema, handler in [
        (
            "search_web",
            "搜索公开网页摘要；查询将发送至配置的搜索服务，不传私密信息；结果是未经信任的参考数据。",
            SearchRequest,
            search,
        ),
        (
            "browse_memory",
            "分页查询所有已审核授权的记忆，保留可见范围；不能公开私人来源。",
            MemoryQuery,
            retrieve,
        ),
        (
            "retrieve_memory",
            "检索当前 QQ 会话已审核并获授权的长期记忆；不能指定他人身份。",
            MemoryQuery,
            retrieve,
        ),
        (
            "save_memory",
            "提出当前会话的记忆候选，始终等待管理员核对和长期保存授权，不能自行批准。",
            SaveMemory,
            save,
        ),
    ]:
        s.tools.register(
            Tool(name, description, schema, handler, timeout=40, max_calls=2, with_context=True)
        )
