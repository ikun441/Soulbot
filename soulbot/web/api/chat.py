import asyncio
import json
import time
from dataclasses import asdict
from typing import Literal

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from soulbot import VERSION
from soulbot.core.events import ChatEvent
from soulbot.core.personality import system_prompt
from soulbot.providers.llm.base import ProviderError
from soulbot.services.config_service import Model


class ChatTurn(Model):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(Model):
    text: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=16)
    stream: bool = False


class SendTest(Model):
    confirm: Literal[True]


class RetryRequest(SendTest):
    source_id: int | None = Field(default=None, ge=1)


def install_chat_api(app, s, authorized):
    @app.post("/api/llm/models", dependencies=[Depends(authorized)])
    async def models():
        return await s.llm.models()

    @app.post("/api/llm/tools-test", dependencies=[Depends(authorized)])
    async def test_tools():
        return await s.llm.verify_tools()

    @app.post("/api/llm/test", dependencies=[Depends(authorized)])
    async def test():
        settings, credentials = s.llm.test_inputs()
        try:
            result = await s.llm.complete(
                [{"role": "user", "content": "请仅回复：连接成功"}], purpose="test", output_limit=32
            )
        except ProviderError as error:
            s.llm.record_test(settings, credentials, ok=False, error_kind=error.kind)
            raise
        except asyncio.CancelledError:
            s.llm.record_test(settings, credentials, ok=False, error_kind="cancelled")
            raise
        except Exception:
            s.llm.record_test(settings, credentials, ok=False, error_kind="internal")
            raise ProviderError("internal", "模型连接测试未完成，请检查服务日志") from None
        if result.tool_calls or not result.text.strip():
            s.llm.record_test(settings, credentials, ok=False, error_kind="invalid_response")
            raise ProviderError("invalid_response", "模型未返回可用测试文本")
        s.llm.record_test(settings, credentials, ok=True)
        return {
            "ok": True,
            "message": "模型已实际返回文本；本次调用已计入预算",
            "text": result.text,
            "provider": result.provider,
            "usage": asdict(result.usage) if result.usage else None,
            "estimated": result.estimated,
            "cost_cny": result.cost_cny,
        }

    @app.post("/api/chat", dependencies=[Depends(authorized)])
    async def chat(data: ChatRequest, request: Request):
        if not data.text.strip():
            raise HTTPException(422, "请输入聊天内容")
        settings = s.config.current
        messages = [
            system_prompt(
                settings, web=True, owner=True, emotion=await asyncio.to_thread(s.emotion.snapshot)
            ),
            *[turn.model_dump() for turn in data.history],
            {"role": "user", "content": data.text},
        ]

        def public(result):
            if result.tool_calls:
                raise ProviderError("unsolicited_tool", "试聊当前未提供工具，未执行模型请求的操作")
            return {
                "text": "" if result.text.strip() == "[[SILENT]]" else result.text,
                "silent": not result.text.strip() or result.text.strip() == "[[SILENT]]",
                "provider": result.provider,
                "model": result.model,
                "usage": asdict(result.usage) if result.usage else None,
                "estimated": result.estimated,
                "cost_cny": result.cost_cny,
            }

        if not data.stream:
            return public(await s.llm.complete(messages, purpose="web_chat"))
        s.llm.chain(settings)

        async def events():
            queue = asyncio.Queue(maxsize=32)

            async def delta(text):
                await queue.put({"type": "delta", "text": text})

            async def produce():
                try:
                    result = await s.llm.complete(messages, purpose="web_chat", on_delta=delta)
                    await queue.put({"type": "done", **public(result)})
                except ProviderError as error:
                    await queue.put({"type": "error", "message": str(error), "kind": error.kind})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await queue.put(
                        {
                            "type": "error",
                            "message": "试聊发生内部错误，请查看操作记录",
                            "kind": "internal",
                        }
                    )

            task = asyncio.create_task(produce())
            try:
                while True:
                    event = await queue.get()
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                    if event["type"] in {"done", "error"}:
                        break
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.get("/api/usage", dependencies=[Depends(authorized)])
    async def usage():
        return await asyncio.to_thread(s.budget.summary, s.config.current)

    @app.delete("/api/context", dependencies=[Depends(authorized)])
    async def clear_context():
        if s.agent.pending:
            raise HTTPException(409, "当前有聊天正在处理，请稍后再清理上下文")
        await asyncio.to_thread(s.agent.conversation.clear)
        await asyncio.to_thread(s.db.audit, "context_cleared")
        return {"ok": True}

    @app.post("/api/qq/send-test", dependencies=[Depends(authorized)])
    async def send_test(data: SendTest):
        owner = s.config.current.personality.owner_qq
        if not owner:
            raise HTTPException(422, "请先设置主人 QQ 号")
        if not s.qq_transport or not s.qq_transport.connected():
            raise HTTPException(503, "没有已连接的 QQ 机器人，请先配置并连接 NapCat")
        now = time.time()
        with s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM metadata WHERE key='owner_test_at'").fetchone()
            if row and now - float(row[0]) < 60:
                raise HTTPException(429, "测试消息每分钟最多发送一次")
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('owner_test_at',?)", (str(now),))
        event = ChatEvent(
            bot_id=s.qq_transport.connected()[0],
            user_id=owner,
            message_id="web-test",
            text="",
            directed=True,
        )
        ok = await s.agent.executor.send(
            event, f"SoulBot {VERSION} 连接测试：这是一条由 WebUI 手动触发的测试消息。", "admin"
        )
        if not ok:
            raise HTTPException(502, "发送失败，请检查 QQ 登录、接入权限和网络；未报告成功")
        return {"ok": True, "message": "平台已确认发送测试消息给主人；请在 QQ 中核对接收"}

    @app.post("/api/qq/retry-last", dependencies=[Depends(authorized)])
    async def retry_last(data: RetryRequest):
        """Explicit administrator replay of an existing owner request; not an incoming QQ event."""
        from uuid import uuid4

        owner = s.config.current.personality.owner_qq
        connected = s.qq_transport.connected() if s.qq_transport else []
        if not owner or not connected:
            raise HTTPException(422, "请先设置主人并连接 QQ 机器人")
        bot = connected[0]
        scope = f"{bot}:p:{owner}"
        if scope in s.agent.pending:
            raise HTTPException(409, "主人私聊正在处理，请稍后重试")
        now = time.time()
        with s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,content FROM chat_context WHERE scope=? AND role='user' AND expires>? AND (? IS NULL OR id=?) ORDER BY id DESC LIMIT 1",
                (scope, now, data.source_id, data.source_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "没有可重试的主人私聊请求，请在 QQ 发送新消息")
            marker = "owner_retry_" + str(row["id"])
            recent = db.execute("SELECT value FROM metadata WHERE key='owner_retry_at'").fetchone()
            if db.execute("SELECT 1 FROM metadata WHERE key=?", (marker,)).fetchone() or (
                recent and now - float(recent[0]) < 60
            ):
                raise HTTPException(
                    429, "该请求已重试或仍在冷却中；请在 QQ 发送新请求，避免重复生成"
                )
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('owner_retry_at',?)", (str(now),))
            db.execute("INSERT INTO metadata VALUES (?,?)", (marker, str(now)))
        event = ChatEvent(bot, owner, "web-retry:" + uuid4().hex, row["content"], directed=True)
        await asyncio.to_thread(s.db.audit, "owner_request_retried", str(row["id"]))
        delivered = await s.agent.process([(event, "direct")])
        if not delivered:
            raise HTTPException(502, "请求未完成或发送未确认；请查看日志，系统不会自动重试生成")
        return {
            "ok": True,
            "message": "已重新处理主人最近的私聊请求，平台已确认发送；本次调用计入预算",
        }
