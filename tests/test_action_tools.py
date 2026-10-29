"""Real tool dispatch and storage regression for scoped diaries and owner draft actions."""

import asyncio
import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from soulbot.app import build_services
from soulbot.core.events import ChatEvent
from soulbot.core.memory import MemoryCreate, MemoryEdit
from soulbot.services.config_service import ConfigService
from soulbot.tools.media_tools import specifications
from soulbot.tools.registry import ToolContext


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    s.now = datetime(2026, 10, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    s.diary.clock = s.qzone.clock = s.memory.clock = lambda: s.now
    cfg = s.config.current
    cfg.qq.enabled = True
    cfg.personality.owner_qq = "23456"
    cfg.diary.enabled = True
    cfg.diary.live_preview = False
    cfg.diary.auto_generate = False
    cfg.qzone.enabled = True
    cfg.qzone.auto_audience = "public"
    return s


def event(user="23456", group=None, bot="34567"):
    return ChatEvent(
        bot, user, uuid4().hex, "保存今天的日记和一份空间草稿", group_id=group, directed=True
    )


def context(e):
    return ToolContext(scope=e.scope, event=e)


def execute(s, name, args, e=None):
    return asyncio.run(s.tools.execute(name, args, context(e or event())))


def record(s, e, count=2):
    for i in range(count):
        s.diary.record(
            ChatEvent(
                e.bot_id,
                e.user_id,
                uuid4().hex,
                "never copy conversation text",
                group_id=e.group_id,
            ),
            has_media=bool(i % 2),
        )


def test_diary_tool_creates_only_current_scope_from_confirmed_counts(s):
    current = event()
    for e in (current, event("45678"), event(group="56789"), event(bot="45678")):
        record(s, e)
    result = execute(s, "create_diary", {}, current)
    assert result["status"] == "saved" and result["facts"]["chat_replies"] == 2
    assert not result["is_final"]
    with s.db.connect() as db:
        rows = db.execute("SELECT scope,facts_json FROM diaries").fetchall()
        assert len(rows) == 1 and rows[0]["scope"] == current.scope
        assert "never copy" not in rows[0]["facts_json"]
        assert db.execute("SELECT COUNT(*) FROM diary_runs").fetchone()[0] == 0


def test_diary_no_activity_cannot_fabricate_or_target_other_people(s):
    assert execute(s, "create_diary", {})["status"] == "skipped"
    for args in ({"content": "今天一起出游"}, {"scope": "34567:p:45678"}, {"bot_id": "45678"}):
        with pytest.raises(ValidationError):
            execute(s, "create_diary", args)
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM diaries").fetchone()[0] == 0


def test_diary_updates_today_idempotently_and_personal_opt_out_stops_readback(s):
    current = event()
    record(s, current)
    first = execute(s, "create_diary", {}, current)
    again = execute(s, "create_diary", {}, current)
    assert first["diary_id"] == again["diary_id"] and not again["created"]
    record(s, current, 1)
    assert execute(s, "create_diary", {}, current)["facts"]["total"] == 3
    s.diary.preference(current, False)
    assert execute(s, "create_diary", {}, current)["status"] == "skipped"
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM diaries").fetchone()[0] == 1
    s.config.current.diary.enabled = False
    with pytest.raises(ValueError, match="启用"):
        execute(s, "create_diary", {}, current)


@pytest.mark.parametrize("day", ["2026-02-30", "tomorrow"])
def test_diary_rejects_invalid_calendar_date(s, day):
    with pytest.raises(ValidationError):
        execute(s, "create_diary", {"day": day})


@pytest.mark.parametrize("day", ["2026-10-29", "2020-01-01"])
def test_diary_rejects_future_or_expired_date(s, day):
    with pytest.raises(ValueError):
        execute(s, "create_diary", {"day": day})


def test_qzone_owner_tool_uses_server_identity_audience_and_persistent_dedup(s):
    current = event()
    s.config.current.qzone.require_approval = False
    result = execute(
        s, "create_qzone_draft", {"content": "秋日的云很轻，愿今天也留一点时间听音乐。"}, current
    )
    row = s.qzone.get(result["draft_id"])
    assert row["bot_id"] == current.bot_id and row["audience"] == "public"
    assert row["source"] == "model_chat_draft" and row["status"] == "pending_approval"
    assert not result["published"] and row["tid"] is None
    again = execute(s, "create_qzone_draft", {"content": row["content"]}, current)
    assert again["draft_id"] == result["draft_id"]
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM qzone_drafts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM qzone_attempts").fetchone()[0] == 0
    s.config.save(s.config.current)
    restarted = build_services(s.root)
    assert (
        execute(restarted, "create_qzone_draft", {"content": row["content"]}, current)["draft_id"]
        == row["id"]
    )
    assert not s.qzone.allowed(row, autonomous=True)


@pytest.mark.parametrize("current", [event("45678"), event(group="56789")])
def test_qzone_draft_forbids_nonowner_and_group(s, current):
    with pytest.raises(PermissionError):
        execute(s, "create_qzone_draft", {"content": "公开的小日常"}, current)
    assert s.qzone.listing()["total"] == 0


def test_model_cannot_choose_bot_audience_owner_or_approval(s):
    for field, value in [
        ("bot_id", "45678"),
        ("audience", "self"),
        ("owner", "45678"),
        ("approved", True),
    ]:
        with pytest.raises(ValidationError):
            execute(s, "create_qzone_draft", {"content": "公开的小日常", field: value})


def test_qzone_tool_blocks_private_memory_and_sensitive_content(s):
    s.config.current.memory.enabled = True
    item = s.memory.create(
        MemoryCreate(bot_id="34567", user_id="45678", content="另一个用户收藏的秘密旅行计划")
    )["item"]
    s.memory.edit(
        item["id"],
        MemoryEdit(revision=0, content=item["content"], status="approved", long_term_allowed=True),
    )
    for content in (item["content"], "联系账号 123456789", "我的密码：不该公开"):
        with pytest.raises(PermissionError):
            execute(s, "create_qzone_draft", {"content": content})
    assert s.qzone.listing()["total"] == 0


def test_new_tools_visibility_and_policy_changes_are_enforced(s):
    def names(e):
        return {item["function"]["name"] for item in specifications(s, e)}

    assert {"create_diary", "create_qzone_draft"} <= names(event())
    assert "create_qzone_draft" not in names(event("45678"))
    assert "create_qzone_draft" not in names(event(group="56789"))
    s.config.current.diary.enabled = s.config.current.qzone.enabled = False
    assert not {"create_diary", "create_qzone_draft"} & names(event())
    s.config.current.tools.create_diary = "deny"
    with pytest.raises(PermissionError):
        execute(s, "create_diary", {})
    s.config.save(s.config.current)
    assert ConfigService(s.root).current.tools.create_diary == "deny"


def test_tool_approval_creates_draft_only_after_review_and_rechecks_feature(s):
    s.config.current.tools.create_qzone_draft = "approval"
    queued = execute(s, "create_qzone_draft", {"content": "来自音乐的小小灵感"})
    assert queued["status"] == "pending" and s.qzone.listing()["total"] == 0
    reviewed = asyncio.run(s.approvals.review(queued["approval_id"], 0, True))
    assert reviewed["result"]["status"] == "saved" and s.qzone.listing()["total"] == 1
    queued = execute(s, "create_qzone_draft", {"content": "另一份公开草稿"})
    s.config.current.qzone.enabled = False
    with pytest.raises(PermissionError):
        asyncio.run(s.approvals.review(queued["approval_id"], 0, True))
    assert s.qzone.listing()["total"] == 1


@pytest.mark.parametrize("finish", ["503", "silent", "rounds", "calls"])
def test_native_model_actions_write_real_records_and_preserve_receipts_on_final_failure(s, finish):
    current = event()
    record(s, current)
    cfg = s.config.current
    cfg.llm.enabled = cfg.llm.supports_tools = True
    cfg.llm.model = "fixture"
    cfg.llm.retries = 0
    cfg.meme.enabled = cfg.emoji.enabled = False
    s.secrets.save({"llm_api_key": "fixture-secret"})
    calls, sent = [], []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) >= 2:
            if finish == "503":
                return httpx.Response(503)
            if finish == "silent":
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": "[[SILENT]]"}}]}
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"again-{i}",
                                        "function": {"name": "create_diary", "arguments": "{}"},
                                    }
                                    for i in range(5 if finish == "calls" else 1)
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                },
            )
        names = {tool["function"]["name"] for tool in body["tools"]}
        assert {"create_diary", "create_qzone_draft"} <= names
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "diary",
                                    "function": {"name": "create_diary", "arguments": "{}"},
                                },
                                {
                                    "id": "draft",
                                    "function": {
                                        "name": "create_qzone_draft",
                                        "arguments": json.dumps(
                                            {"content": "今天也想听一首轻快的歌。"}
                                        ),
                                    },
                                },
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    async def sender(event, text):
        sent.append(text)

    s.llm.transport = httpx.MockTransport(respond)
    s.agent.executor.sender = sender
    assert asyncio.run(s.agent.process([(current, "direct")]))
    assert len(calls) == (4 if finish == "rounds" else 2) and sent
    assert "本地日记" in "".join(sent) and "空间草稿" in "".join(sent)
    assert s.qzone.listing()["total"] == 1
    with s.db.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM diaries WHERE scope=?", (current.scope,)).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM qzone_attempts").fetchone()[0] == 0


def test_memory_context_leaves_room_for_tool_schema_and_current_request(s):
    cfg = s.config.current
    cfg.memory.enabled = True
    cfg.memory.max_chars = 4000
    cfg.llm.enabled = cfg.llm.supports_tools = True
    cfg.llm.model = "fixture"
    cfg.limits.max_input_per_request = 2000
    cfg.llm.context_window = 2400
    s.secrets.save({"llm_api_key": "fixture-secret"})
    for text, importance in [("长记忆内容" * 180, 5), ("小记忆参考", 3)]:
        item = s.memory.create(
            MemoryCreate(bot_id="34567", user_id="23456", content=text, importance=importance)
        )["item"]
        s.memory.edit(
            item["id"],
            MemoryEdit(
                revision=0,
                content=text,
                importance=importance,
                status="approved",
                long_term_allowed=True,
            ),
        )

    def respond(request):
        body = json.loads(request.content)
        assert "小记忆参考" in body["messages"][0]["content"]
        assert "长记忆内容" not in body["messages"][0]["content"]
        assert body["messages"][-1] == {"role": "user", "content": "保存日记"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 2},
            },
        )

    s.llm.transport = httpx.MockTransport(respond)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "noop",
                "description": "safe" * 120,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    result = asyncio.run(
        s.llm.complete(
            [{"role": "system", "content": "test"}, {"role": "user", "content": "保存日记"}],
            tools=tools,
        )
    )
    assert result.text == "ok"
    # The larger entry remains retrievable; only automatic injection was bounded.
    assert s.memory.recall()["total"] == 2


def test_comfy_model_tool_describes_prompt_language_without_affecting_cloud(s):
    cfg = s.config.current.image_generation
    cfg.enabled, cfg.provider, cfg.comfy.checkpoint = True, "comfyui", "fixture.safetensors"

    def description():
        return next(
            tool["function"]["description"]
            for tool in specifications(s, event())
            if tool["function"]["name"] == "generate_image"
        )

    assert "英文" in description() and "猫与猫娘不同" in description()
    cfg.comfy.tool_prompt_language = "native"
    assert "英文" not in description()
    cfg.provider, cfg.model = "openai_compatible", "fixture"
    s.secrets.save({"image_api_key": "fixture-secret"})
    assert "英文" not in description()


def test_prepared_whisper_model_is_reported_ready_after_restart(tmp_path):
    from soulbot.providers.asr.local import FILES, REVISION, LocalASRProvider

    root = tmp_path / "data/asr"
    (root / "base").mkdir(parents=True)
    for name in FILES:
        (root / "base" / name).write_bytes(b"fixture")
    (root / "ready.json").write_text(
        json.dumps({"revision": REVISION, "sizes": {name: 7 for name in FILES}})
    )
    provider = LocalASRProvider(tmp_path)
    assert provider.ready() and "已准备" in provider.message


def test_tool_discovery_cannot_grant_new_tools(s):
    e = event()
    ctx = context(e)
    ctx.available_tools = frozenset({"create_diary"})
    result = asyncio.run(s.tools.execute("discover_tools", {"names": ["create_diary"]}, ctx))
    assert result["selected"] == ["create_diary"]
    with pytest.raises(PermissionError):
        asyncio.run(
            s.tools.execute("discover_tools", {"names": ["unregistered_shell"]}, context(e))
        )
    with pytest.raises(ValidationError):
        asyncio.run(s.tools.execute("discover_tools", {"names": ["a", "b", "c", "d"]}, context(e)))
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM diaries").fetchone()[0] == 0


def test_small_model_context_loads_only_selected_authorized_tool(s):
    from soulbot.core.personality import system_prompt

    cfg = s.config.current
    cfg.llm.enabled = cfg.llm.supports_tools = True
    cfg.llm.context_window = 6000
    cfg.llm.model = "fixture"
    cfg.llm.retries = 0
    cfg.tts.enabled = cfg.html_render.enabled = cfg.memory.enabled = cfg.search.enabled = True
    cfg.image_generation.enabled = True
    cfg.image_generation.provider = "comfyui"
    cfg.image_generation.comfy.checkpoint = "fixture.safetensors"
    s.renderer.ready = lambda: True
    s.secrets.save({"llm_api_key": "fixture-secret"})
    e = event()
    record(s, e)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body["tools"]}
        calls.append(names)
        if len(calls) == 1:
            assert names == {"discover_tools"}
            name, arguments = "discover_tools", {"names": ["create_diary"]}
        elif len(calls) == 2:
            assert "create_diary" in names and "generate_image" not in names
            name, arguments = "create_diary", {}
        else:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "日记已保存。"}}],
                    "usage": {"prompt_tokens": 200, "completion_tokens": 20},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": name,
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 200, "completion_tokens": 20},
            },
        )

    s.llm.transport = httpx.MockTransport(respond)
    result, attachments = asyncio.run(
        s.agent.answer(
            [
                system_prompt(cfg, private=True, owner=True),
                {"role": "user", "content": "保存今天日记"},
            ],
            e,
            "explicit",
        )
    )
    assert result.text == "日记已保存。" and not attachments
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM diaries").fetchone()[0] == 1


def test_budget_warning_only_survives_restart_and_deduplicates(s):
    from soulbot.core.budget import BudgetService

    cfg = s.config.current
    cfg.behavior.daily_tokens = 1000
    cfg.behavior.daily_cost_cny = 0.01
    cfg.limits.max_input_per_request = 256
    cfg.limits.max_per_agent_task = 512
    cfg.llm.input_price_cny_per_million = cfg.llm.output_price_cny_per_million = 1000
    assert s.budget.reserve(cfg, cfg.llm, "primary", "proactive", 2000, 500, task_tokens=2500)
    assert s.budget.reserve(
        cfg, cfg.llm, "primary", "qzone_autonomous", 2000, 500, task_tokens=5000
    )
    summary = BudgetService(s.db).summary(cfg)
    assert summary["warning_only"] and summary["reserved_or_used_tokens"] == 5000
    assert {item["code"] for item in summary["recent_warnings"]} >= {
        "daily_tokens",
        "daily_cost",
        "request_input",
        "task_tokens",
    }
    with s.db.connect() as db:
        assert (
            max(
                row[0]
                for row in db.execute(
                    "SELECT COUNT(*) FROM audit WHERE action='budget_warning' GROUP BY detail"
                )
            )
            == 1
        )
