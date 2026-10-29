"""Model tools have server-owned identities and cannot grant memory consent or publish."""

import asyncio
import hashlib
from datetime import date
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, field_validator

from soulbot.core.memory import MemoryCreate
from soulbot.services.config_service import Model
from soulbot.services.qzone_service import DraftCreate
from soulbot.services.search_service import SearchRequest
from soulbot.tools.registry import Tool


class MemoryQuery(Model):
    query: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0, le=1000000)


class SaveMemory(Model):
    content: str = Field(min_length=1, max_length=1000)
    importance: int = Field(default=3, ge=1, le=5)


class DiscoverTools(Model):
    names: list[str] = Field(min_length=1, max_length=3)


class CreateDiary(Model):
    day: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("day")
    @classmethod
    def valid_day(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value


class CreateQZoneDraft(Model):
    content: str = Field(min_length=1, max_length=2000)
    clean = field_validator("content")(DraftCreate.clean.__func__)


def register_social_tools(s):
    async def search(args, context):
        return await s.search.search(args, context.purpose)

    def event(context):
        if context.event is None or context.event.scope != context.scope:
            raise PermissionError("工具需要系统提供当前 QQ 会话")
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

    def accessible(context):
        current = event(context)
        if not s.agent.decision.access(s.config.current, current):
            raise PermissionError("当前 QQ 会话权限已关闭")
        return current

    async def diary(args, context):
        current = accessible(context)
        return await asyncio.to_thread(s.diary.create_for_event, current, args.day)

    async def qzone_draft(args, context):
        current = accessible(context)
        cfg = s.config.current
        if not cfg.qzone.enabled or current.group_id or current.user_id != cfg.personality.owner_qq:
            raise PermissionError("只有主人私聊可以请求空间草稿，请启用空间功能")
        if s.qzone.content_issues(args.content):
            raise PermissionError("草稿包含疑似私人信息，未保存")
        digest = hashlib.sha256(args.content.encode()).hexdigest()
        request_id = uuid5(NAMESPACE_URL, "soulbot:qzone-draft:" + current.key + ":" + digest)
        row = await s.qzone.create(
            DraftCreate(
                request_id=request_id,
                bot_id=current.bot_id,
                content=args.content,
                audience=cfg.qzone.auto_audience,
            ),
            from_chat=True,
        )
        published = row["status"] == "published" and bool(row["tid"])
        message = (
            f"这条 QQ 空间草稿（ID {row['id']}）已有平台发布回执，未重复创建或再次发布。"
            if published
            else f"已保存 QQ 空间草稿（ID {row['id']}），当前状态为{row['status_label']}；工具没有发布它，可在 WebUI 的 QQ 空间页查看，并按当前发布规则处理。"
        )
        return {
            "status": "saved",
            "draft_id": row["id"],
            "draft_status": row["status"],
            "audience": row["audience"],
            "published": published,
            "message": message,
        }

    async def discover(args, context):
        names = list(dict.fromkeys(args.names))
        if not set(names) <= context.available_tools:
            raise PermissionError("只能选择当前会话已有权限的工具")
        return {"selected": names, "message": "仅载入工具参数说明，没有执行这些工具或授予新权限。"}

    for name, description, schema, handler in [
        (
            "discover_tools",
            "按需选择本轮所需工具，每次最多3项；只载入参数说明，不执行外部操作。",
            DiscoverTools,
            discover,
        ),
        (
            "create_diary",
            "根据当前会话已确认的真实活动保存本地日记；省略日期为今天。不接受编造正文，不推送或发布。",
            CreateDiary,
            diary,
        ),
        (
            "create_qzone_draft",
            "仅限主人私聊：把适合公开的正文保存为 QQ 空间草稿，未发布。账号与受众由系统配置，不得引用私人记忆。",
            CreateQZoneDraft,
            qzone_draft,
        ),
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
            Tool(
                name,
                description,
                schema,
                handler,
                timeout=40,
                max_calls=1 if name in {"create_diary", "create_qzone_draft"} else 2,
                with_context=True,
            )
        )
