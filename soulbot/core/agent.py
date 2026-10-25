import asyncio
from dataclasses import dataclass, field

from soulbot.core.conversation import ConversationStore
from soulbot.core.decision import DecisionEngine
from soulbot.core.permissions import PermissionEngine
from soulbot.core.personality import system_prompt
from soulbot.providers.llm.base import ProviderError


class ActionExecutor:
    def __init__(self, config, decision, audit):
        self.config, self.decision, self.audit = config, decision, audit
        self.sender = None
        self.lock = asyncio.Lock()
        self.permissions = PermissionEngine({"send_text": "auto"})

    async def send(self, event, text, trigger):
        if not self.sender:
            return False
        self.permissions.require("send_text")
        async with self.lock:
            if not await self.allowed(event, trigger):
                return False
            settings = self.config.current
            delay = await asyncio.to_thread(self.decision.reserve_send, settings)
            if delay:
                await asyncio.sleep(delay)
                if not await self.allowed(event, trigger):
                    return False
                await asyncio.to_thread(self.decision.reserve_send, self.config.current)
            if not await self.allowed(event, trigger):
                return False
            try:
                async with asyncio.timeout(10):
                    await self.sender(event, text[:2000])
            except Exception:
                await asyncio.to_thread(self.audit, "qq_send_failed", trigger)
                return False
        await asyncio.to_thread(self.audit, "qq_sent", trigger)
        return True

    async def allowed(self, event, trigger):
        settings = self.config.current
        if not await asyncio.to_thread(self.decision.access, settings, event):
            return False
        if event.group_id and trigger != "admin":
            state = await asyncio.to_thread(self.decision.state, event)
            if (
                state["muted_until"] > self.decision.clock()
                or self.decision.mode(settings, event) == "silent"
            ):
                return False
            if trigger == "proactive":
                policy = self.decision.group_policy(settings, event)
                if (
                    not settings.behavior.proactive_group_enabled
                    or self.decision.quiet(settings)
                    or not policy
                    or not policy.allow_proactive
                    or self.decision.mode(settings, event) not in {"natural", "active"}
                ):
                    return False
        return True


@dataclass
class Pending:
    items: list = field(default_factory=list)
    task: asyncio.Task | None = None


class SoulBotAgent:
    def __init__(self, config, db, llm):
        self.config, self.db, self.llm = config, db, llm
        self.conversation = ConversationStore(db)
        self.decision = DecisionEngine(db, self.conversation)
        self.executor = ActionExecutor(config, self.decision, db.audit)
        self.pending = {}
        self.closed = False

    async def accept(self, event):
        if self.closed:
            return
        if not await asyncio.to_thread(self.conversation.unseen, event):
            return
        if await self.admin_command(event):
            return
        trigger = await asyncio.to_thread(self.decision.classify, self.config.current, event)
        if not trigger:
            return
        if event.scope not in self.pending:
            if len(self.pending) >= 256:
                await asyncio.to_thread(self.db.audit, "message_queue_full")
                return
            pending = Pending()
            self.pending[event.scope] = pending
            pending.items.append((event, trigger))
            pending.task = asyncio.create_task(
                self.worker(event.scope), name="soulbot-message-batch"
            )
        elif len(self.pending[event.scope].items) < 20:
            self.pending[event.scope].items.append((event, trigger))

    async def worker(self, scope):
        try:
            pending = self.pending[scope]
            while pending.items:
                event = pending.items[0][0]
                settings = self.config.current
                await asyncio.sleep(
                    settings.behavior.group_batch_seconds
                    if event.group_id
                    else settings.behavior.private_batch_seconds
                )
                batch = pending.items[:]
                pending.items.clear()
                try:
                    await self.process(batch)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.to_thread(self.db.audit, "agent_failed", "internal")
        finally:
            self.pending.pop(scope, None)

    async def process(self, batch):
        settings = self.config.current.model_copy(deep=True)
        eligible = []
        for event, old_trigger in batch:
            current = await asyncio.to_thread(self.decision.classify, settings, event, False)
            if current and (old_trigger != "proactive" or current == "proactive" or event.directed):
                eligible.append((event, current))
        if not eligible:
            return
        event = eligible[-1][0]
        trigger = (
            "direct"
            if any(t == "direct" for _, t in eligible)
            else "followup"
            if any(t == "followup" for _, t in eligible)
            else "proactive"
        )
        if trigger == "proactive":
            policy = self.decision.group_policy(settings, event)
            if (
                not settings.behavior.proactive_group_enabled
                or not policy
                or not policy.allow_proactive
                or self.decision.quiet(settings)
            ):
                return
        if not await asyncio.to_thread(self.decision.claim, settings, event, trigger):
            return
        if not self.llm.configured():
            await asyncio.to_thread(self.db.audit, "llm_not_configured")
            return
        text = "\n".join(item.text for item, _ in eligible)
        history = await asyncio.to_thread(
            self.conversation.history, event.scope, settings.behavior.context_turns
        )
        messages = [
            system_prompt(
                settings,
                private=not event.group_id,
                owner=event.user_id == settings.personality.owner_qq,
            ),
            *history,
            {"role": "user", "content": text},
        ]
        try:
            result = await self.llm.complete(
                messages, purpose="proactive" if trigger == "proactive" else "explicit"
            )
            if result.tool_calls:
                raise ProviderError("unsolicited_tool", "当前没有向模型提供工具，未执行任何操作")
            if not result.text.strip() or result.text.strip() == "[[SILENT]]":
                await asyncio.to_thread(self.db.audit, "agent_silent", trigger)
                return
            if await self.executor.send(event, result.text, trigger):
                await asyncio.to_thread(
                    self.conversation.remember, event.scope, text, result.text[:2000], settings
                )
                if trigger == "proactive":
                    await asyncio.to_thread(self.decision.proactive_sent, event)
        except ProviderError as error:
            await asyncio.to_thread(self.db.audit, "agent_model_error", error.kind)

    async def admin_command(self, event):
        if not event.text.strip().startswith("/soulbot"):
            return False
        settings = self.config.current
        owner = (
            bool(settings.personality.owner_qq) and event.user_id == settings.personality.owner_qq
        )
        administrator = event.group_id and event.role in {"admin", "owner"}
        if not (owner or administrator) or not await asyncio.to_thread(
            self.decision.access, settings, event
        ):
            return True
        if not await asyncio.to_thread(self.decision.claim, settings, event, "admin"):
            return True
        command = event.text.strip().split()
        reply = "命令：/soulbot 状态、/soulbot 静音 15、/soulbot 恢复（群管理员或主人可用）"
        if command[1:] == ["状态"]:
            reply = "SoulBot 正在运行。群聊规则、冷却与预算已启用；人格和情绪属于程序模拟。"
        elif event.group_id and command[1:] == ["恢复"]:
            await asyncio.to_thread(self.decision.mute, event, 0)
            reply = "已恢复本群回复。"
        elif (
            event.group_id
            and len(command) == 3
            and command[1] == "静音"
            and command[2].isdigit()
            and 1 <= int(command[2]) <= 1440
        ):
            await asyncio.to_thread(self.decision.mute, event, int(command[2]) * 60)
            reply = f"已在本群静音 {command[2]} 分钟，可使用 /soulbot 恢复。"
        await self.executor.send(event, reply, "admin")
        return True

    async def close(self):
        self.closed = True
        tasks = [p.task for p in list(self.pending.values()) if p.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
