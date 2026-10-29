"""Integration regressions for remaining features: actual handlers, isolated providers and restart state."""

import asyncio
import io
import json
import zipfile
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.core.memory import MemoryCreate, MemoryEdit
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import Completion, ProviderError, Usage
from soulbot.services.backup_service import BackupService, InstanceLock
from soulbot.services.diary_periods import PolishDiary
from soulbot.services.search_service import SearchRequest
from soulbot.tools.registry import ToolContext


def stamp(day):
    return (
        datetime.fromisoformat(day + "T12:00:00")
        .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        .timestamp()
    )


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    s.now = [stamp("2026-10-26")]
    s.diary.clock = s.search.clock = s.scheduler.clock = s.consent.clock = s.memory.clock = lambda: (
        s.now[0]
    )
    s.config.current.diary.enabled = True
    s.config.current.diary.live_preview = False
    s.config.current.diary.auto_generate = False
    return s


def evt(user="23456", group=None):
    return ChatEvent("34567", user, uuid4().hex, "test", group_id=group, directed=True)


def add_day(s, day, user="23456", group=None):
    s.now[0] = stamp(day)
    for _ in range(2):
        s.diary.record(evt(user, group))
    s.now[0] = stamp("2026-10-26")
    s.diary.generate(day)


def approved(s, user, content, group=""):
    created = s.memory.create(
        MemoryCreate(bot_id="34567", user_id=user, group_id=group, content=content)
    )["item"]
    return s.memory.edit(
        created["id"],
        MemoryEdit(revision=0, content=content, status="approved", long_term_allowed=True),
    )


def search_fixture(s, handler=None):
    s.config.current.search.enabled = True
    s.config.current.search.price_cny_per_request = 0

    def default(request):
        assert request.url.path == "/search" and request.url.params["format"] == "json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "<b>Public</b>",
                        "url": "https://example.com/article",
                        "content": "<i>Fact</i>",
                    },
                    {"title": "private", "url": "http://127.0.0.1/a", "content": "hidden"},
                ]
            },
        )

    s.search.transport = httpx.MockTransport(handler or default)


def test_search_actual_contract_filter_budget_and_restart(s):
    search_fixture(s)
    result = asyncio.run(s.search.search(SearchRequest(query="公开主题")))
    assert result["items"] == [
        {"title": "Public", "url": "https://example.com/article", "snippet": "Fact"}
    ]
    assert s.budget.summary(s.config.current)["search"]["recent"][0]["cost_cny"] == 0
    s.config.current.search.max_calls_per_day = 1
    with pytest.raises(ProviderError, match="额度"):
        asyncio.run(s.search.search(SearchRequest(query="again")))
    again = build_services(s.root)
    with again.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM search_usage").fetchone()[0] == 1


@pytest.mark.parametrize("status", [403, 429, 503])
def test_search_errors_keep_costs_and_never_echo_upstream(s, status):
    search_fixture(s, lambda _: httpx.Response(status, text="do not echo upstream-secret"))
    with pytest.raises(ProviderError) as error:
        asyncio.run(s.search.search(SearchRequest(query="公开主题")))
    assert "upstream-secret" not in str(error.value)
    with s.db.connect() as db:
        assert db.execute("SELECT status FROM search_usage").fetchone()[0] == "uncertain"


def test_exploration_disabled_unknown_cost_paused_and_deduplicated(s):
    search_fixture(s)
    s.config.current.exploration.enabled = True
    s.config.current.search.price_cny_per_request = None
    asyncio.run(s.search.tick())
    assert s.search.notes()["state"]["status"] == "completed"
    assert len(s.search.notes()["items"]) == 1
    s.now[0] += 3 * 3600
    s.config.current.search.price_cny_per_request = 0
    asyncio.run(s.search.tick())
    assert len(s.search.notes()["items"]) == 2
    asyncio.run(s.search.tick())
    assert len(s.search.notes()["items"]) == 2
    s.config.current.automation.paused = True
    s.now[0] += 3 * 3600
    asyncio.run(s.search.tick())
    assert len(s.search.notes()["items"]) == 2


def test_exploration_rechecks_policy_after_slow_search(s):
    search_fixture(s)
    s.config.current.exploration.enabled = True

    async def changed(request):
        s.config.current.automation.paused = True
        return httpx.Response(200, json={"results": []})

    s.search.transport = httpx.MockTransport(changed)
    asyncio.run(s.search.tick())
    assert not s.search.notes()["items"]
    assert s.search.notes()["state"]["status"] == "policy_changed"


def test_weekly_scope_idempotency_and_personal_deletion(s):
    for user in ["23456", "45678"]:
        for day in ["2026-10-19", "2026-10-20"]:
            add_day(s, day, user)
    assert s.periods.generate_week("2026-10-20")["created"] == 2
    assert s.periods.generate_week("2026-10-21")["created"] == 0
    rows = s.periods.summaries()["items"]
    assert all(r["facts"]["total"] == 4 for r in rows)
    s.diary.forget(evt())
    assert len(s.periods.summaries()["items"]) == 1
    assert s.periods.summaries()["items"][0]["scope"].endswith("45678")


def test_weekly_requires_ended_week_two_days_and_one_timezone(s):
    add_day(s, "2026-10-19")
    assert s.periods.generate_week("2026-10-20")["created"] == 0
    with pytest.raises(ValueError):
        s.periods.generate_week("2026-10-26")
    add_day(s, "2026-10-20")
    with s.db.connect() as db:
        db.execute("UPDATE diaries SET timezone='UTC' WHERE day='2026-10-20'")
    assert s.periods.generate_week("2026-10-20")["created"] == 0


def test_delete_day_invalidates_weekly_facts(s):
    for day in ["2026-10-19", "2026-10-20"]:
        add_day(s, day)
    s.periods.generate_week("2026-10-19")
    item = s.diary.listing()["items"][0]
    s.diary.delete(item["id"], item["revision"])
    assert not s.periods.summaries()["items"]


def test_polish_uses_only_facts_preserves_facts_and_rejects_duplicate(s):
    add_day(s, "2026-10-19")
    item = s.diary.listing()["items"][0]
    calls = []

    async def complete(messages, **kwargs):
        calls.append(messages)
        assert "private-message-body" not in json.dumps(messages)
        assert "prompt-must-not-be-sent" not in json.dumps(messages)
        return Completion("今天记下两次确实发生的交流。", Usage(20, 10))

    s.config.current.personality.prompt = "prompt-must-not-be-sent"
    s.llm.complete = complete
    data = PolishDiary(request_id=uuid4(), revision=item["revision"])
    polished = asyncio.run(s.periods.polish(item["id"], data))
    assert polished["facts"] == item["facts"] and polished["role_source"] == "model"
    with pytest.raises(StateConflict):
        asyncio.run(s.periods.polish(item["id"], data))
    assert len(calls) == 1


def test_polish_does_not_overwrite_concurrent_edit_or_deletion(s):
    add_day(s, "2026-10-19")
    item = s.diary.listing()["items"][0]

    async def complete(*args, **kwargs):
        s.diary.delete(item["id"], item["revision"])
        return Completion("must not revive deleted diary", Usage(1, 1))

    s.llm.complete = complete
    with pytest.raises(StateConflict):
        asyncio.run(s.periods.polish(item["id"], PolishDiary(request_id=uuid4(), revision=0)))
    assert not s.diary.listing()["items"]


def test_delivery_only_owner_facts_and_real_consent_review_queue(s):
    s.config.current.personality.owner_qq = "23456"
    s.config.current.diary.delivery_bot_id = "34567"
    add_day(s, "2026-10-19")
    add_day(s, "2026-10-19", "45678")
    with pytest.raises(ValueError, match="授权"):
        s.periods.delivery("daily", "2026-10-19")
    s.consent.set_user(evt(), consent=True)
    result = s.periods.delivery("daily", "2026-10-19")
    assert result["item"]["status"] == "pending_approval"
    assert "2 次" in result["item"]["content"] and "45678" not in result["item"]["content"]
    assert s.periods.delivery("daily", "2026-10-19")["duplicate"]
    s.config.current.automation.paused = True
    with pytest.raises(ValueError):
        s.periods.delivery("daily", "2026-10-19")


def test_global_memory_filters_consent_expiry_and_preferences(s):
    s.config.current.memory.enabled = True
    approved(s, "23456", "主人喜欢宁静的天文话题")
    approved(s, "45678", "另一位用户喜欢音乐和绘画", "56789")
    s.memory.create(MemoryCreate(bot_id="34567", user_id="56789", content="未经审核的秘密"))
    assert s.memory.recall()["total"] == 2
    assert len(s.memory.recall(offset=1, limit=1)["items"]) == 1
    s.memory.preference(evt("23456"), False)
    result = s.memory.recall()["items"]
    assert len(result) == 1 and result[0]["visibility"] == "group"
    assert s.memory.private_echo(result[0]["content"])
    assert not s.memory.private_echo(result[0]["content"], result[0]["source_scope"])
    s.config.current.memory.global_context = False
    assert not s.memory.recall()["items"]


def test_global_memory_enters_every_llm_action_without_cross_scope_approval(s):
    s.config.current.memory.enabled = True
    approved(s, "23456", "主人喜欢宁静的天文话题")
    seen = []

    async def complete(messages, *args, **kwargs):
        seen.append(messages)
        return Completion("hello", Usage(1, 1))

    s.llm._complete = complete
    for purpose in ["qzone_draft", "diary_polish", "explicit", "proactive"]:
        result = asyncio.run(
            s.llm.complete(
                [{"role": "system", "content": "test"}, {"role": "user", "content": "天文"}],
                purpose=purpose,
            )
        )
        assert result.text == "hello"
    assert len(seen) == 4
    assert all("主人喜欢宁静的天文话题" in json.dumps(m, ensure_ascii=False) for m in seen)
    assert all(m[1]["role"] == "user" for m in seen)


def test_memory_tool_never_approves_or_invents_identity(s):
    s.config.current.memory.enabled = True
    context = ToolContext(scope=evt().scope, event=evt())
    result = asyncio.run(s.tools.execute("save_memory", {"content": "明确的候选事实"}, context))
    row = s.memory.listing()["items"][0]
    assert (
        row["status"] == "candidate"
        and not row["long_term_allowed"]
        and result["candidate_id"] == row["id"]
    )
    with pytest.raises(PermissionError):
        asyncio.run(
            s.tools.execute(
                "save_memory", {"content": "cannot fake identity"}, ToolContext(scope="admin")
            )
        )


def test_private_memory_echo_blocks_external_tool_and_qzone(s):
    s.config.current.memory.enabled = True
    approved(s, "45678", "某个用户的私人长期记忆内容")
    with pytest.raises(PermissionError):
        asyncio.run(
            s.tools.execute(
                "search_web",
                {"query": "某个用户的私人长期记忆内容"},
                ToolContext(scope=evt().scope, event=evt()),
            )
        )
    assert "包含其它会话记忆原文" in s.qzone.content_issues("某个用户的私人长期记忆内容")


def test_backup_snapshot_validates_wal_restores_state_revokes_session_preserves_new_limits(s):
    s.auth.setup("tester", "test-password-at-least-12")
    token = s.auth.session()
    s.db.set("fixture", "before")
    backup = BackupService(s.root)
    content = backup.snapshot()
    assert {"config.yaml", ".env", "data/soulbot.db"} <= backup.validate(content).keys()
    s.db.set("fixture", "after")
    with s.db.connect() as db:
        db.execute(
            "INSERT INTO search_usage VALUES ('post-snapshot','2026-10-26','explicit','completed',0.5,?)",
            (s.now[0],),
        )
    backup.stage(content)
    with InstanceLock(s.root) as lock:
        assert backup.apply_pending(lock)
    again = build_services(s.root)
    assert again.db.get("fixture") == "before"
    assert again.config.current.automation.paused
    assert not again.auth.valid(token)
    with again.db.connect() as db:
        assert (
            db.execute("SELECT cost_cny FROM search_usage WHERE id='post-snapshot'").fetchone()[0]
            == 0.5
        )
    assert list((s.root / "data/backups").glob("before-restore-*.zip"))


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "emoji/../../escape.png", "data/unknown", "emoji\\escape.png"],
)
def test_backup_rejects_unknown_paths_without_writing(s, name):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr(name, b"bad")
    with pytest.raises(ValueError):
        BackupService(s.root).stage(out.getvalue())
    assert not (s.root / "data/restore-pending.zip").exists()


def test_backup_checksum_and_instance_lock(s):
    backup = BackupService(s.root)
    original = backup.snapshot()
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    files["config.yaml"] = b"invalid"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError):
        backup.validate(out.getvalue())
    with InstanceLock(s.root):
        with pytest.raises(ValueError, match="运行"):
            with InstanceLock(s.root):
                pass


def test_new_api_auth_templates_import_stop_search_and_backup(s):
    client = TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"})
    for path in [
        "/api/personality/templates",
        "/api/diaries/weekly",
        "/api/exploration",
        "/api/backups/status",
        "/api/openapi.json",
    ]:
        assert client.get(path).status_code == 401
    client.post(
        "/api/auth/setup", json={"username": "tester", "password": "test-password-at-least-12"}
    )
    payload = client.get("/api/personality/templates").json()["items"][1]
    payload.pop("id")
    assert client.put("/api/personality", json=payload).status_code == 200
    assert "owner_qq" not in client.get("/api/personality/export").json()
    assert client.get("/api/personality/preview").json()["role"] == "system"
    assert client.post("/api/automation/stop").json()["paused"]
    assert client.post("/api/search", json={"query": "test"}).status_code == 422
    content = client.post("/api/backups").content
    assert zipfile.is_zipfile(io.BytesIO(content))
    assert (
        client.post("/api/backups/restore", files={"file": ("safe.zip", content)}).status_code
        == 200
    )
    assert client.get("/api/backups/status").json()["pending_restore"]
    assert client.delete("/api/backups/restore").status_code == 200
    assert not client.get("/api/backups/status").json()["pending_restore"]


def test_tool_approval_is_owner_bound_single_use_and_real_execution(s):
    s.config.current.personality.owner_qq = "23456"
    s.config.current.tools.search_emoji = "approval"
    context = ToolContext(scope=evt().scope, event=evt())
    result = asyncio.run(s.tools.execute("search_emoji", {"query": "test"}, context))
    assert result["status"] == "pending"
    item = s.approvals.listing()["items"][0]
    assert item["status"] == "pending"
    completed = asyncio.run(s.approvals.review(item["id"], item["revision"], True))
    assert completed["status"] == "completed" and "items" in completed["result"]
    with pytest.raises(StateConflict):
        asyncio.run(s.approvals.review(item["id"], item["revision"], True))


def test_tool_approval_rechecks_revocation_and_owner_change(s):
    s.config.current.personality.owner_qq = "23456"
    s.config.current.tools.search_emoji = "approval"
    asyncio.run(
        s.tools.execute(
            "search_emoji", {"query": "test"}, ToolContext(scope=evt().scope, event=evt())
        )
    )
    item = s.approvals.listing()["items"][0]
    s.config.current.tools.search_emoji = "deny"
    with pytest.raises(StateConflict):
        asyncio.run(s.approvals.review(item["id"], 0, True))
    s.config.current.tools.search_emoji = "approval"
    s.config.current.personality.owner_qq = "45678"
    with pytest.raises(StateConflict):
        asyncio.run(s.approvals.review(item["id"], 0, True))


def test_ai_proactive_candidate_requires_consent_budget_and_review(s):
    s.config.current.qq.enabled = True
    s.config.current.proactive.enabled = True
    s.config.current.proactive.auto_drafts = True
    s.config.current.personality.owner_qq = "23456"
    s.qq_transport = SimpleNamespace(connected=lambda: ["34567"])
    calls = []

    async def complete(*args, **kwargs):
        calls.append(kwargs["purpose"])
        return Completion("你好。今天想聊聊天吗？不方便回复也没关系。", Usage(1, 1))

    s.llm.complete = complete
    asyncio.run(s.scheduler.tick())
    assert not calls
    s.consent.set_user(evt(), consent=True)
    asyncio.run(s.scheduler.tick())
    assert calls == ["proactive"]
    item = s.scheduler.listing()["items"][0]
    assert item["status"] == "pending_approval"
    asyncio.run(s.scheduler.tick())
    assert calls == ["proactive"]
    s.config.current.automation.paused = True
    assert s.qzone.auto_reason() == "disabled"


def test_ai_proactive_revocation_during_generation_never_queues(s):
    s.config.current.qq.enabled = True
    s.config.current.proactive.enabled = s.config.current.proactive.auto_drafts = True
    s.qq_transport = SimpleNamespace(connected=lambda: ["34567"])
    s.consent.set_user(evt(), consent=True)

    async def complete(*args, **kwargs):
        s.consent.set_user(evt(), consent=False)
        return Completion("do not send", Usage(1, 1))

    s.llm.complete = complete
    asyncio.run(s.scheduler.tick())
    assert not s.scheduler.listing()["items"]


def test_action_memory_readonly_tool_loop_has_shared_budget_and_validated_pages(s):
    s.config.current.memory.enabled = True
    approved(s, "23456", "主人喜欢宁静的天文话题")
    s.llm.chain = lambda *args: [("fixture", None, None)]
    calls = []

    async def complete(messages, *args, **kwargs):
        calls.append(messages[:])
        if len(calls) == 1:
            return Completion(
                "",
                Usage(1, 1),
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "retrieve_memory",
                            "arguments": '{"query":"天文","offset":0}',
                        },
                    }
                ],
            )
        assert messages[-1]["role"] == "tool"
        assert "主人喜欢宁静的天文话题" in messages[-1]["content"]
        return Completion("依据已授权记忆作出判断", Usage(1, 1))

    s.llm._complete = complete
    result = asyncio.run(
        s.llm.complete(
            [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}],
            purpose="qzone_draft",
        )
    )
    assert len(calls) == 2 and result.text == "依据已授权记忆作出判断"


def test_restore_keeps_current_tool_approval_decision(s):
    from soulbot.tools.media_tools import SearchEmoji

    s.config.current.personality.owner_qq = "23456"
    s.config.save(s.config.current)
    context = ToolContext(scope=evt().scope, event=evt())
    key = s.approvals.enqueue("search_emoji", SearchEmoji(query="开心"), context)["approval_id"]
    backup = BackupService(s.root)
    snapshot = backup.snapshot()
    assert asyncio.run(s.approvals.review(key, 0, False))["status"] == "rejected"
    backup.stage(snapshot)
    with InstanceLock(s.root) as lock:
        backup.apply_pending(lock)
    again = build_services(s.root)
    assert again.approvals.listing()["items"][0]["status"] == "rejected"
