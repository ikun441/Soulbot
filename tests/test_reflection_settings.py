"""Persistent reflection, memory preflight and category selection integration."""

import asyncio
import json
import time
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.core.memory import MemoryCreate, MemoryEdit
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import Completion
from soulbot.services.media_service import MediaError
from soulbot.services.reflection_service import ExtractedMemory
from soulbot.tools.registry import ToolContext


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    s.now = [time.time()]
    for service in [s.reflection, s.memory, s.emotion, s.relationships]:
        service.clock = lambda: s.now[0]
    cfg = s.config.current
    cfg.qq.enabled = True
    cfg.memory.enabled = True
    cfg.reflection.enabled = True
    cfg.reflection.cooldown_seconds = 60
    cfg.llm.model = "fixture-model"
    s.secrets.save({"llm_api_key": "fixture-only-key"})
    return s


def event(text="我喜欢轻音乐，谢谢你的耐心回答", user="23456", group=None):
    return ChatEvent("34567", user, uuid4().hex, text, group_id=group, directed=True)


def approve(s, content, e):
    row = s.memory.create(
        MemoryCreate(bot_id=e.bot_id, user_id=e.user_id, group_id=e.group_id or "", content=content)
    )["item"]
    return s.memory.edit(
        row["id"],
        MemoryEdit(
            content=content, revision=row["revision"], status="approved", long_term_allowed=True
        ),
    )


def result(text="我喜欢轻音乐"):
    return {
        "memories": [
            {"content": "用户喜欢轻音乐", "evidence": text, "kind": "person", "importance": 3}
        ],
        "feedback": {
            "signal": "positive",
            "target": "bot",
            "confidence": 0.95,
            "evidence": "谢谢你的耐心回答",
        },
    }


def model(s, monkeypatch, payload, hook=None):
    calls = []

    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        if hook:
            hook()
        return Completion(json.dumps(payload, ensure_ascii=False), None)

    monkeypatch.setattr(s.llm, "complete", complete)
    return calls


def test_reflection_candidates_have_evidence_and_do_not_auto_approve(s, monkeypatch):
    e = event()
    calls = model(s, monkeypatch, result())
    s.relationships.interact(e)
    assert s.reflection.enqueue(e)
    assert not s.reflection.enqueue(e)
    asyncio.run(s.reflection.tick())
    items = s.memory.listing()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "candidate" and not item["long_term_allowed"]
    assert item["evidence"] == "我喜欢轻音乐" and item["user_id"] == e.user_id
    assert s.memory.retrieve(e) == []
    assert calls[0][1]["include_memory"] is False and calls[0][1]["tools"] == []
    assert s.relationships.get(e)["affinity"] >= 1
    with s.db.connect() as db:
        row = db.execute("SELECT * FROM reflection_jobs").fetchone()
        assert row["status"] == "completed" and not row["input_text"]
    asyncio.run(s.reflection.tick())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"memories": [{"content": "编造的经历", "evidence": "这段原文并不存在"}]},
        {
            "memories": [],
            "feedback": {"signal": "admin", "target": "bot", "confidence": 1, "evidence": "test"},
        },
        {"memories": [], "shell": "forbidden"},
    ],
)
def test_bad_model_structure_or_unanchored_evidence_never_creates_memory(s, monkeypatch, payload):
    calls = model(s, monkeypatch, payload)
    assert s.reflection.enqueue(event())
    asyncio.run(s.reflection.tick())
    asyncio.run(s.reflection.tick())
    assert not s.memory.listing()["items"] and len(calls) == 1
    assert s.reflection.status()["items"][0]["status"] == "failed"


@pytest.mark.parametrize("change", ["disable", "forget", "optout", "pause"])
def test_mid_request_revocation_prevents_old_memory_write(s, monkeypatch, change):
    e = event()

    def hook():
        if change == "disable":
            s.config.current.reflection.enabled = False
        elif change == "pause":
            s.config.current.automation.paused = True
        elif change == "forget":
            s.memory.forget(e)
        else:
            s.memory.preference(e, False)

    model(s, monkeypatch, result(), hook)
    assert s.reflection.enqueue(e)
    asyncio.run(s.reflection.tick())
    assert not s.memory.listing()["items"]
    with s.db.connect() as db:
        assert not db.execute("SELECT 1 FROM reflection_jobs WHERE input_text!=''").fetchone()


def test_reflection_daily_attempts_and_cooldown_survive_restart(s, monkeypatch):
    s.config.current.reflection.max_daily_tasks = 1
    calls = model(s, monkeypatch, {"memories": []})
    e = event()
    assert s.reflection.enqueue(e)
    assert not s.reflection.enqueue(event())
    asyncio.run(s.reflection.tick())
    s.now[0] += 61
    assert s.reflection.enqueue(event())
    asyncio.run(s.reflection.tick())
    assert len(calls) == 1
    s.config.save(s.config.current)
    reopened = build_services(s.root)
    reopened.reflection.clock = lambda: s.now[0]
    assert reopened.reflection.claim() is None
    s.memory.forget(e)
    assert s.reflection.claim() is None


def test_restart_does_not_repeat_inflight_analysis(s):
    assert s.reflection.enqueue(event())
    row = s.reflection.claim()
    assert row

    async def run():
        await s.reflection.startup()
        await s.reflection.close()

    asyncio.run(run())
    assert s.reflection.status()["items"][0]["status"] == "uncertain"


def test_proposed_update_requires_explicit_review_and_conflict_check(s):
    e = event()
    old = approve(s, "用户喜欢轻音乐", e)
    assert s.reflection.enqueue(e)
    job = s.reflection.claim()
    candidate = s.memory.propose(
        e,
        ExtractedMemory(content="用户现在喜欢钢琴音乐", evidence="我喜欢轻音乐"),
        "我喜欢轻音乐",
        job["id"],
    )["item"]
    assert candidate["proposed_target_id"] == old["id"]
    assert s.memory.listing(status="approved")["items"][0]["id"] == old["id"]
    edited = s.memory.edit(
        old["id"],
        MemoryEdit(
            content=old["content"],
            revision=old["revision"],
            status="approved",
            long_term_allowed=True,
        ),
    )
    with pytest.raises(StateConflict):
        s.memory.edit(
            candidate["id"],
            MemoryEdit(
                content=candidate["content"],
                revision=candidate["revision"],
                status="approved",
                long_term_allowed=True,
                replace_target=True,
            ),
        )
    assert edited["status"] == "approved"


def test_confirmed_proposal_archives_only_matching_old_record(s):
    e = event()
    old = approve(s, "用户喜欢轻音乐", e)
    assert s.reflection.enqueue(e)
    job = s.reflection.claim()
    candidate = s.memory.propose(
        e,
        ExtractedMemory(content="用户现在喜欢钢琴音乐", evidence="我喜欢轻音乐"),
        "我喜欢轻音乐",
        job["id"],
    )["item"]
    s.memory.edit(
        candidate["id"],
        MemoryEdit(
            content=candidate["content"],
            revision=candidate["revision"],
            status="approved",
            long_term_allowed=True,
            replace_target=True,
        ),
    )
    assert [r["id"] for r in s.memory.listing(status="approved")["items"]] == [candidate["id"]]
    assert [r["id"] for r in s.memory.listing(status="archived")["items"]] == [old["id"]]


def test_negative_feedback_limited_frozen_and_idempotent(s):
    e = event()
    s.relationships.interact(e)
    initial = s.relationships.get(e)["affinity"]
    for _ in range(8):
        current = event()
        s.relationships.apply_feedback(current, "negative")
        assert not s.relationships.apply_feedback(current, "negative")
        s.now[0] += 301
    assert (
        s.relationships.get(e)["affinity"]
        == initial - s.config.current.relationships.max_daily_loss
    )
    row = s.relationships.get(e)
    s.relationships.edit(row["id"], row["revision"], display_name="", affinity=20, automatic=False)
    assert not s.relationships.apply_feedback(event(), "negative")
    assert s.relationships.get(e)["affinity"] == 20
    s.config.current.emotion.automatic = False
    before = s.emotion.snapshot()["values"]
    assert not s.emotion.apply_feedback(event(), "negative")
    assert s.emotion.snapshot()["values"] == before


def test_action_memory_checks_local_global_and_respects_optout(s):
    e = event()
    approve(s, "用户喜欢轻音乐", e)
    other = event(user="45678", group="56789")
    approve(s, "群内聊过的公开音乐话题", other)
    refs = s.action_memory.retrieve(e, "音乐", "fixture")
    assert len(refs["local"]) == 1 and len(refs["global"]) == 2
    s.memory.preference(e, False)
    refs = s.action_memory.retrieve(e, "音乐", "fixture")
    assert refs["local"] == [] and len(refs["global"]) == 1
    s.config.current.action_memory.global_memory = False
    assert s.action_memory.retrieve(e, "音乐", "fixture")["global"] == []


def png():
    stream = BytesIO()
    Image.new("RGB", (8, 8), "pink").save(stream, "PNG")
    return stream.getvalue()


def test_category_first_tool_flow_and_memory_preflight(s):
    s.emoji.add_category("猫猫贴贴")
    first = s.emoji.upload("抱抱.png", png(), "猫猫贴贴")
    s.emoji.upload("其他.png", png(), "其他")
    ctx = ToolContext(scope=event().scope, event=event())

    async def run():
        categories = await s.tools.execute("list_emoji_categories", {"query": "贴贴"}, ctx)
        assert categories["items"] == [{"name": "猫猫贴贴", "count": 1}]
        page = await s.tools.execute("browse_emoji", {"category": "猫猫贴贴", "limit": 1}, ctx)
        assert page["total"] == 1 and page["items"][0]["id"] == first["id"]
        chosen = await s.tools.execute("select_emoji", {"emoji_id": page["items"][0]["id"]}, ctx)
        assert chosen["attachment"] == "emoji:" + first["id"]
        assert ctx.memory_references["enabled"]

    asyncio.run(run())


@pytest.mark.parametrize("name", ["../escape", ".hidden", "a/b", "a\\b", "\x00bad", ""])
def test_category_rejects_path_and_hidden_names(s, name):
    with pytest.raises(MediaError):
        s.emoji.add_category(name)


def test_emoji_watch_updates_deletions_and_skips_symlinks(s, tmp_path):
    s.config.current.emoji.watch_changes = True
    assert s.emoji.scan_changes()
    assert not s.emoji.scan_changes()
    p = s.emoji.root / "开心" / "测试.png"
    p.write_bytes(png())
    assert s.emoji.scan_changes()
    assert s.emoji.browse(category="开心")["total"] == 1
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    (s.emoji.root / "开心" / "link.png").symlink_to(outside)
    s.emoji.scan_changes()
    assert s.emoji.browse(category="开心")["total"] == 1
    p.unlink()
    assert s.emoji.scan_changes()
    assert s.emoji.browse(category="开心")["total"] == 0


def test_settings_and_category_api_are_authenticated_and_persistent(s):
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as c:
        assert c.get("/api/settings/memory").status_code == 401
        assert c.get("/api/reflection/status").status_code == 401
        assert c.post("/api/emoji/categories", json={"name": "新的分类"}).status_code == 401
        c.post("/api/auth/setup", json={"username": "fixture", "password": "fixture-password-123"})
        assert c.post("/api/emoji/categories", json={"name": "新的分类"}).json()["created"]
        assert any(r["name"] == "新的分类" for r in c.get("/api/emoji/categories").json()["items"])
        cfg = c.get("/api/config").json()["config"]
        assert cfg["action_memory"] == {"enabled": True, "local": True, "global_memory": True}
        cfg["reflection"]["enabled"] = False
        assert c.put("/api/config", json=cfg).status_code == 200
    assert build_services(s.root).config.current.action_memory.enabled
