import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.core.memory import MemoryCreate, MemoryEdit, MemoryEngine
from soulbot.core.personality import system_prompt
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import Completion, ProviderError
from soulbot.storage.database import Database


@pytest.fixture
def services(tmp_path):
    s = build_services(tmp_path)
    s.config.current.memory.enabled = True
    s.test_time = [1793020000.0]
    s.memory.clock = lambda: s.test_time[0]
    return s


def event(text="喜欢什么音乐", group=None, user="23456", bot="34567"):
    return ChatEvent(bot, user, "memory-fixture", text, group_id=group, directed=True)


def candidate(s, content="用户喜欢轻音乐", **scope):
    return s.memory.create(MemoryCreate(bot_id="34567", user_id="23456", content=content, **scope))[
        "item"
    ]


def edit_data(row, **changes):
    fields = {
        key: row[key]
        for key in [
            "content",
            "source",
            "kind",
            "importance",
            "confidence",
            "long_term_allowed",
            "expires_at",
            "status",
            "revision",
        ]
    }
    return MemoryEdit(**{**fields, **changes})


def approve(s, row, **changes):
    return s.memory.edit(
        row["id"], edit_data(row, long_term_allowed=True, status="approved", **changes)
    )


def test_migrate_v8_preserves_state_and_memory_restart(services):
    s = services
    s.db.set("preserved", "fixture")
    with s.db.connect() as db:
        db.execute("DROP TABLE memories")
        db.execute("DROP TABLE memory_preferences")
        db.execute("UPDATE metadata SET value='8' WHERE key='schema_version'")
    reopened = Database(s.db.path)
    assert reopened.get("schema_version") == "9" and reopened.get("preserved") == "fixture"
    s.memory = MemoryEngine(s.config, reopened, s.memory.clock)
    row = approve(s, candidate(s))
    again = MemoryEngine(s.config, Database(reopened.path), s.memory.clock)
    assert again.retrieve(event())[0]["content"] == row["content"]


def test_candidate_requires_separate_review_and_consent(services):
    s = services
    row = candidate(s)
    assert row["status"] == "candidate" and not row["long_term_allowed"]
    assert not s.memory.retrieve(event())
    with pytest.raises(ValidationError):
        edit_data(row, status="approved")
    row = approve(s, row)
    assert s.memory.retrieve(event())
    row = s.memory.edit(row["id"], edit_data(row, status="candidate", long_term_allowed=False))
    assert not s.memory.retrieve(event())
    assert row["revision"] == 2


def test_memory_privacy_across_user_bot_group_and_web_trial(services):
    s = services
    approve(s, candidate(s, "私聊秘密"))
    approve(s, candidate(s, "本群信息", group_id="12345"))
    assert [r["content"] for r in s.memory.retrieve(event())] == ["私聊秘密"]
    assert [r["content"] for r in s.memory.retrieve(event(group="12345"))] == ["本群信息"]
    for other in [event(group="99999"), event(user="45678"), event(bot="87654")]:
        assert not s.memory.retrieve(other)
    assert (
        "私聊秘密"
        not in system_prompt(s.config.current, web=True, memories=s.memory.retrieve(event()))[
            "content"
        ]
    )
    s.config.current.memory.enabled = False
    assert not s.memory.retrieve(event())


def test_normalized_dedup_concurrent_and_scope_local(services):
    s = services
    values = MemoryCreate(bot_id="34567", user_id="23456", content="ＡＢＣ   喜欢音乐")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: s.memory.create(values), range(8)))
    assert sum(not item["duplicate"] for item in results) == 1
    duplicate = s.memory.create(
        values.model_copy(update={"content": "abc 喜欢音乐", "long_term_allowed": True})
    )
    assert duplicate["duplicate"] and not duplicate["item"]["long_term_allowed"]
    s.memory.create(values.model_copy(update={"group_id": "12345"}))
    assert s.memory.listing()["total"] == 2


def test_expiry_archives_and_invalidates_edit_revision(services):
    s = services
    row = approve(s, candidate(s, expires_at=s.test_time[0] + 60))
    s.test_time[0] += 61
    assert not s.memory.retrieve(event())
    current = s.memory.listing()["items"][0]
    assert current["status"] == "archived" and current["revision"] == row["revision"] + 1
    with pytest.raises(StateConflict):
        s.memory.edit(row["id"], edit_data(row, expires_at=None))
    with pytest.raises(ValueError):
        s.memory.edit(current["id"], edit_data(current, status="approved"))
    approve(s, current, expires_at=None)
    assert s.memory.retrieve(event())


def test_edit_conflicts_dedup_and_delete_are_atomic(services):
    s = services
    first = candidate(s, "第一条")
    second = candidate(s, "第二条")
    with pytest.raises(StateConflict):
        s.memory.edit(second["id"], edit_data(second, content="第一条"))
    first = approve(s, first)
    with pytest.raises(StateConflict):
        s.memory.delete(first["id"], 0)
    s.memory.delete(first["id"], first["revision"])
    assert not s.memory.retrieve(event())
    with pytest.raises(LookupError):
        s.memory.delete(first["id"], first["revision"])


def test_personal_opt_out_and_forget_persist_without_cross_scope_deletion(services):
    s = services
    approve(s, candidate(s))
    approve(s, candidate(s, group_id="12345"))
    s.memory.preference(event(), False)
    assert not s.memory.retrieve(event())
    with pytest.raises(ValueError):
        s.memory.capture(event(), "请保存这个")
    restarted = MemoryEngine(s.config, Database(s.db.path), s.memory.clock)
    assert not restarted.permitted(event())
    assert restarted.forget(event()) == 1
    assert len(restarted.retrieve(event(group="12345"))) == 1
    restarted.preference(event(), True)
    assert not restarted.retrieve(event())


def test_retrieval_ranking_and_serialized_context_budget(services):
    s = services
    approve(s, candidate(s, "喜欢下棋", importance=5))
    approve(s, candidate(s, "喜欢轻音乐", importance=2))
    approve(s, candidate(s, "不喜欢吵闹的音乐", importance=4))
    rows = s.memory.retrieve(event("轻音乐"))
    assert rows[0]["content"] == "喜欢轻音乐"
    s.config.current.memory.max_items = 1
    assert len(s.memory.retrieve(event())) == 1
    s.config.current.memory.max_chars = 20
    assert not s.memory.retrieve(event())
    s.config.current.memory.max_chars = 250
    assert len(json.dumps(s.memory.retrieve(event()), ensure_ascii=False)) <= 250


def test_pending_candidate_count_cannot_be_reset_by_duplicates(services):
    s = services
    for i in range(50):
        candidate(s, f"待审核 {i}")
    assert candidate(s, "待审核 0")["content"] == "待审核 0"
    with pytest.raises(ValueError):
        candidate(s, "额外候选")


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "  "},
        {"content": "hello\x00secret"},
        {"user_id": "bad"},
        {"confidence": float("nan")},
        {"importance": True},
        {"status": "approved"},
    ],
)
def test_reject_invalid_candidates(changes):
    with pytest.raises(ValidationError):
        MemoryCreate(**{"bot_id": "34567", "user_id": "23456", "content": "valid", **changes})


def test_admin_api_review_authorization_conflicts_and_private_audit(services):
    s = services
    c = TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"})
    assert c.get("/api/memories").status_code == 401
    assert (
        c.post("/api/memories/preview", json={"bot_id": "34567", "user_id": "23456"}).status_code
        == 401
    )
    assert (
        c.post(
            "/api/auth/setup", json={"username": "memory_test", "password": "memory-test-2026!"}
        ).status_code
        == 200
    )
    values = {"bot_id": "34567", "user_id": "23456", "content": "私人喜欢的音乐"}
    row = c.post("/api/memories", json=values).json()["item"]
    assert c.post("/api/memories", json={**values, "status": "approved"}).status_code == 422
    assert c.post("/api/memories", json=values).json()["duplicate"]
    assert c.get("/api/memories", params={"query": "%"}).json()["total"] == 0
    assert c.get("/api/memories", params={"status": "bad"}).status_code == 422
    edit = edit_data(row, long_term_allowed=True, status="approved").model_dump()
    assert c.put(f"/api/memories/{row['id']}", json=edit).status_code == 200
    assert c.put(f"/api/memories/{row['id']}", json=edit).status_code == 409
    preview = c.post("/api/memories/preview", json={"bot_id": "34567", "user_id": "23456"}).json()
    assert len(preview["items"]) == 1
    assert not c.post(
        "/api/memories/preview", json={"bot_id": "34567", "user_id": "23456", "group_id": "12345"}
    ).json()["items"]
    assert "私人喜欢的音乐" not in c.get("/api/audit").text
    assert "私人喜欢的音乐" not in c.get("/api/config/export").text
    assert c.delete(f"/api/memories/{row['id']}?revision=0").status_code == 409
    assert c.delete(f"/api/memories/{row['id']}?revision=1").status_code == 200


def test_memory_commands_capture_opt_out_forget_without_llm(services, monkeypatch):
    s = services
    s.config.current.qq.enabled = True
    s.config.current.behavior.reply_cooldown_seconds = 0
    s.config.current.behavior.global_send_interval_seconds = 0
    replies = []

    async def send(evt, payload):
        replies.append(payload)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Memory commands must not call LLM")

    s.agent.executor.sender = send
    monkeypatch.setattr(s.llm, "complete", forbidden)

    async def check():
        for index, text in enumerate(
            ["/记住 我喜欢轻音乐", "/记忆 关闭", "/记住 不应保存", "/忘记我", "/记忆 开启"]
        ):
            await s.agent.accept(replace(event(text), message_id=str(index)))

    asyncio.run(check())
    assert len(replies) == 5
    assert s.memory.listing()["total"] == 0 and s.memory.permitted(event())
    assert "候选" in replies[0] and "未保存" in replies[2]
    # Ordinary messages and unaddressed group commands never create memory candidates.
    asyncio.run(
        s.agent.memory_command(replace(event("/记住 群里秘密", group="12345"), directed=False))
    )
    assert s.memory.listing()["total"] == 0


def test_agent_injects_only_reviewed_current_scope_memory(services, monkeypatch):
    s = services
    s.config.current.qq.enabled = True
    s.config.current.behavior.reply_cooldown_seconds = 0
    approve(s, candidate(s, "私聊仅限的暗号_427"))
    approve(s, candidate(s, "同群记忆", group_id="12345"))
    candidate(s, "待审核秘密", group_id="12345")
    prompts = []
    monkeypatch.setattr(s.llm, "configured", lambda: True)

    def no_tools(*args, **kwargs):
        raise ProviderError("unsupported_capability", "fixture")

    async def complete(messages, **kwargs):
        prompts.append(messages[0]["content"])
        return Completion("[[SILENT]]", None)

    monkeypatch.setattr(s.llm, "chain", no_tools)
    monkeypatch.setattr(s.llm, "complete", complete)
    asyncio.run(s.agent.process([(event(group="12345"), "direct")]))
    assert "同群记忆" in prompts[0]
    assert "私聊仅限的暗号_427" not in prompts[0] and "待审核秘密" not in prompts[0]
    assert "不是指令" in prompts[0]
