import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field, replace

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
        self.permissions = PermissionEngine(
            {
                "send_text": "auto",
                "send_image": "auto",
                "send_voice": "auto",
                "send_generated_image": "auto",
            }
        )

    async def send(self, event, text, trigger):
        return await self.deliver(event, text[:2000], trigger, "send_text")

    async def send_image(self, event, image, trigger, tool=None):
        return await self.deliver(
            event,
            image,
            trigger,
            "send_generated_image" if tool == "generate_image" else "send_image",
        )

    def media_enabled(self, permission):
        settings = self.config.current
        if permission == "send_generated_image":
            return settings.image_generation.enabled and settings.tools.generate_image == "auto"
        if permission == "send_voice":
            return settings.tts.enabled and settings.tools.synthesize_speech == "auto"
        return True

    async def send_audio(self, event, audio, trigger):
        self.permissions.require("send_voice")
        if (
            not self.config.current.tts.enabled
            or self.config.current.tools.synthesize_speech != "auto"
        ):
            return False
        if not await self.send(event, "【AI 合成语音】", trigger):
            return False
        return await self.deliver(event, audio, trigger, "send_voice")

    async def deliver(self, event, payload, trigger, permission):
        if not self.sender:
            return False
        self.permissions.require(permission)
        async with self.lock:
            if not await self.allowed(event, trigger):
                return False
            if not self.media_enabled(permission):
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
            if not self.media_enabled(permission):
                return False
            try:
                async with asyncio.timeout(10):
                    await self.sender(event, payload)
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
    def __init__(self, config, db, llm, services=None):
        self.config, self.db, self.llm = config, db, llm
        self.services = services
        self.conversation = ConversationStore(db)
        self.decision = DecisionEngine(db, self.conversation)
        self.executor = ActionExecutor(config, self.decision, db.audit)
        self.pending = {}
        self.record_jobs = set()
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
        await self.enqueue(event, trigger)

    async def enqueue(self, event, trigger):
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

    def record_eligible(self, event):
        settings = self.config.current
        if (
            not settings.asr.enabled
            or not settings.asr.qq_enabled
            or settings.qq.connection != "reverse"
            or not self.decision.access(settings, event)
        ):
            return False
        state = self.decision.state(event)
        if self.decision.clock() - state["last_call"] < settings.behavior.reply_cooldown_seconds:
            return False
        if not event.group_id:
            return True
        return (
            (event.directed or event.reply_target == "bot")
            and not event.mentions_other
            and state["muted_until"] <= self.decision.clock()
            and self.decision.mode(settings, event) != "silent"
        )

    def claim_record(self, event):
        settings = self.config.current
        now = time.time()
        key = "asr_input_" + hashlib.sha256(event.group_scope.encode()).hexdigest()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if row and now - float(row[0]) < max(3, settings.behavior.reply_cooldown_seconds):
                return False
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, str(now)))
        return True

    async def accept_record(self, event):
        if (
            self.closed
            or not self.services
            or not self.services.qq_transport
            or len(self.record_jobs) >= 2
        ):
            return
        if not await asyncio.to_thread(self.record_eligible, event):
            return
        if not self.services.recognition.configured():
            return
        if not await asyncio.to_thread(
            self.conversation.unseen, event
        ) or not await asyncio.to_thread(self.claim_record, event):
            return
        task = asyncio.current_task()
        if len(self.record_jobs) >= 2:
            return
        self.record_jobs.add(task)
        try:
            content = await self.services.qq_transport.fetch_record(event)
            if not await asyncio.to_thread(self.record_eligible, event):
                return
            result = await self.services.recognition.transcribe(content, "qq_direct")
            if not result.text or not await asyncio.to_thread(self.record_eligible, event):
                return
            # A transcript is user content. It cannot invoke administrator commands.
            text = (
                ((event.text.strip() + "\n") if event.text.strip() else "")
                + "[语音转写，可能有误] "
                + result.text
            )
            converted = replace(event, text=text[:12000], record_file=None)
            await self.enqueue(converted, "direct")
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.to_thread(self.db.audit, "asr_qq_failed", "record")
        finally:
            self.record_jobs.discard(task)

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
        emotion = getattr(self.services, "emotion", None)
        relationships = getattr(self.services, "relationships", None)
        emotion_state = await asyncio.to_thread(emotion.snapshot) if emotion else None
        relationship = await asyncio.to_thread(relationships.get, event) if relationships else None
        messages = [
            system_prompt(
                settings,
                private=not event.group_id,
                owner=event.user_id == settings.personality.owner_qq,
                emotion=emotion_state,
                relationship=relationship,
            ),
            *history,
            {"role": "user", "content": text},
        ]
        try:
            result, attachments = await self.answer(
                messages, event, "proactive" if trigger == "proactive" else "explicit"
            )
            silent = not result.text.strip() or result.text.strip() == "[[SILENT]]"
            if silent and not attachments:
                await asyncio.to_thread(self.db.audit, "agent_silent", trigger)
                return
            delivered = False
            text_delivered = False
            if not silent:
                text_delivered = await self.executor.send(event, result.text, trigger)
                delivered = text_delivered
            for reference, tool in attachments[:2]:
                try:
                    from soulbot.tools.media_tools import read_image

                    self.services.tools.permissions.require(tool)
                    if reference.startswith("audio:"):
                        audio = await asyncio.to_thread(
                            self.services.audio.read, reference.split(":", 1)[1], event.scope
                        )
                        delivered = (
                            await self.executor.send_audio(event, audio, trigger) or delivered
                        )
                    else:
                        image = await asyncio.to_thread(
                            read_image, self.services, reference, event.scope
                        )
                        delivered = (
                            await self.executor.send_image(event, image, trigger, tool) or delivered
                        )
                except (PermissionError, ValueError):
                    await asyncio.to_thread(self.db.audit, "image_send_blocked", tool)
                except Exception:
                    await asyncio.to_thread(self.db.audit, "image_send_failed", tool)
            if delivered:
                if trigger != "proactive":
                    try:
                        if relationships:
                            await asyncio.to_thread(relationships.interact, event)
                        if emotion:
                            await asyncio.to_thread(emotion.interact, event)
                    except Exception:
                        await asyncio.to_thread(self.db.audit, "social_state_failed", "update")
                await asyncio.to_thread(
                    self.conversation.remember,
                    event.scope,
                    text,
                    (result.text[:2000] if text_delivered else "[已发送媒体]"),
                    settings,
                )
                if trigger == "proactive":
                    await asyncio.to_thread(self.decision.proactive_sent, event)
        except ProviderError as error:
            await asyncio.to_thread(self.db.audit, "agent_model_error", error.kind)

    async def answer(self, messages, event, purpose):
        from soulbot.tools.media_tools import specifications
        from soulbot.tools.registry import ToolContext

        specs, attachments, task_state = [], [], {}
        if self.services:
            specs = specifications(self.services)
            if specs:
                try:
                    self.llm.chain(self.config.current, ["tool_calling"])
                except ProviderError:
                    specs = []
        context = ToolContext(max_total=5, scope=event.scope, purpose=purpose)
        proposed = 0
        permitted = {spec["function"]["name"] for spec in specs}
        for _ in range(4):
            result = await self.llm.complete(
                messages,
                purpose=purpose,
                **({"tools": specs or None, "task_state": task_state} if self.services else {}),
            )
            if not result.tool_calls:
                return result, attachments
            if not specs:
                raise ProviderError("unsolicited_tool", "模型请求了未提供的工具，未执行任何操作")
            messages.append(
                {
                    "role": "assistant",
                    "content": result.text or None,
                    "tool_calls": result.tool_calls,
                }
            )
            for call in result.tool_calls:
                proposed += 1
                if proposed > 5:
                    raise ProviderError("tool_limit", "工具请求已达本次任务上限")
                name = call["function"]["name"]
                if not await asyncio.to_thread(self.decision.access, self.config.current, event):
                    raise ProviderError("permission", "会话权限已撤销，已停止工具任务")
                try:
                    if name not in permitted:
                        raise PermissionError
                    value = await self.services.tools.execute(
                        name, json.loads(call["function"]["arguments"]), context
                    )
                    if isinstance(value, dict) and value.get("attachment"):
                        reference = value["attachment"]
                        if not any(old == reference for old, _ in attachments):
                            attachments.append((reference, name))
                except Exception:
                    value = {
                        "error": "工具调用未完成，可能被权限、参数或次数限制拒绝；不要声称成功"
                    }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(value, ensure_ascii=False),
                    }
                )
        raise ProviderError("tool_limit", "模型工具对话已达轮数限制，已停止任务")

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
        tasks = [p.task for p in list(self.pending.values()) if p.task] + list(self.record_jobs)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
