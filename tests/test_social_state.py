import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.core.emotion import EmotionEngine, choose_state
from soulbot.core.events import ChatEvent
from soulbot.core.personality import system_prompt
from soulbot.core.relationship import RelationshipEngine
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import Completion, ProviderError
from soulbot.services.config_service import EmotionSettings, EmotionValues, Settings
from soulbot.storage.database import Database


@pytest.fixture
def services(tmp_path):
    value = build_services(tmp_path)
    clock = [1793000000.0]
    value.emotion.clock = value.relationships.clock = lambda: clock[0]
    value.test_clock = clock
    return value


def event(message="one", *, group=None, user="23456", bot="34567"):
    return ChatEvent(
        bot, user, message, "private text not for social storage", group_id=group, directed=True
    )


def test_v7_migration_preserves_existing_state_and_is_idempotent(services):
    services.db.set("preserved", "fixture")
    with services.db.connect() as db:
        db.execute("DROP TABLE emotion_state")
        db.execute("DROP TABLE emotion_history")
        db.execute("DROP TABLE relationships")
        db.execute("DROP TABLE social_observations")
        db.execute("UPDATE metadata SET value='7' WHERE key='schema_version'")
    reopened = Database(services.db.path)
    assert reopened.get("schema_version") == "13" and reopened.get("preserved") == "fixture"
    Database(reopened.path)
    assert EmotionEngine(services.config, reopened).snapshot()["state"] == "calm"


@pytest.mark.parametrize(
    "changes,state",
    [
        ({"fatigue": 90}, "tired"),
        ({"energy": 10}, "tired"),
        ({"valence": 10}, "withdrawn"),
        ({"sociability": 10}, "withdrawn"),
        ({"valence": 85, "energy": 85}, "excited"),
        ({"curiosity": 85}, "curious"),
        ({"valence": 85}, "content"),
        ({}, "calm"),
    ],
)
def test_state_machine_states_priorities_and_hysteresis(changes, state):
    policy = EmotionSettings()
    values = {**EmotionValues().model_dump(), **changes}
    assert choose_state(values, policy, force=True) == state
    assert choose_state({**values, "fatigue": 90}, policy, force=True) == "tired"
    assert choose_state(values, policy, "calm", 1000, 1020) == "calm"
    tired = {**EmotionValues().model_dump(), "fatigue": 70}
    assert choose_state(tired, policy, "tired", 0, 1000) == "tired"
    tired["fatigue"] = 64
    assert choose_state(tired, policy, "tired", 0, 1000) == "calm"


def test_emotion_decay_restart_freeze_and_policy_changes(services):
    engine = services.emotion
    edited = engine.edit({**EmotionValues().model_dump(), "fatigue": 90}, 0)
    assert edited["state"] == "tired"
    services.test_clock[0] += 180 * 60
    restarted = EmotionEngine(services.config, services.db, engine.clock)
    state = restarted.snapshot()
    assert state["values"]["fatigue"] == pytest.approx(50) and state["state"] == "calm"
    services.config.current.emotion.automatic = False
    frozen = restarted.snapshot()
    services.test_clock[0] += 3600 * 48
    assert restarted.snapshot()["values"] == frozen["values"]
    services.config.current.emotion.automatic = True
    assert restarted.snapshot()["values"] == frozen["values"]
    services.test_clock[0] += 180 * 60
    assert restarted.snapshot()["values"]["fatigue"] == pytest.approx(30)


def test_emotion_success_events_are_deduplicated_and_globally_cooled(services):
    engine = services.emotion
    engine.interact(event())
    after = engine.snapshot()
    engine.interact(event())
    engine.interact(event("two", user="45678"))
    assert engine.snapshot()["values"] == after["values"]
    services.test_clock[0] += 61
    engine.interact(event("three"))
    assert engine.snapshot()["revision"] == after["revision"] + 1
    assert engine.snapshot()["values"]["fatigue"] > after["values"]["fatigue"]
    with pytest.raises(StateConflict):
        engine.edit(EmotionValues().model_dump(), after["revision"])
    assert len(engine.history()) == 2
    assert "private text" not in json.dumps(engine.history())


def test_relationship_identity_isolates_bots_groups_and_private_context(services):
    engine = services.relationships
    private = event()
    engine.interact(private)
    assert engine.get(private)["affinity"] == 1
    for other in [
        replace(private, group_id="12345"),
        replace(private, bot_id="98765"),
        replace(private, user_id="87654"),
    ]:
        assert engine.get(other) is None
        engine.interact(other)
    assert engine.listing()["total"] == 4
    row = engine.get(private)
    engine.edit(
        row["id"], row["revision"], display_name="private-only-name", affinity=88, automatic=True
    )
    assert engine.get(replace(private, group_id="12345"))["display_name"] == ""
    group_prompt = system_prompt(
        services.config.current, relationship=engine.get(replace(private, group_id="12345"))
    )
    assert "private-only-name" not in group_prompt["content"]


def test_affinity_daily_limit_cooldown_edits_restart_and_timezone_boundary(services):
    engine = services.relationships
    # Local midnight should reset the date, not process startup or administrator edits.
    now = datetime(2026, 10, 26, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    services.test_clock[0] = now
    services.config.current.relationships.max_daily_gain = 2
    engine.interact(event())
    engine.interact(event())
    assert engine.get(event())["interactions"] == 1
    engine.interact(event("two"))
    assert engine.get(event())["interactions"] == 2
    assert engine.get(event())["affinity"] == 1
    services.test_clock[0] += 301
    engine.interact(event("three"))
    row = engine.get(event())
    engine.edit(row["id"], row["revision"], display_name="user", affinity=60, automatic=True)
    services.test_clock[0] += 301
    restarted = RelationshipEngine(services.config, services.db, engine.clock)
    restarted.interact(event("four"))
    assert restarted.get(event())["affinity"] == 60
    assert restarted.get(event())["daily_gain"] == 2
    services.test_clock[0] = now + 3601
    restarted.interact(event("next-day"))
    assert restarted.get(event())["affinity"] == 61
    assert restarted.get(event())["daily_gain"] == 1


def test_concurrent_rewards_and_per_user_freeze_are_atomic(services):
    engine = services.relationships
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: engine.interact(event(str(i))), range(12)))
    row = engine.get(event())
    assert row["affinity"] == 1 and row["interactions"] == 12
    frozen = engine.edit(
        row["id"], row["revision"], display_name="frozen", affinity=100, automatic=False
    )
    services.test_clock[0] += 3601
    engine.interact(event("late"))
    assert engine.get(event()) == frozen
    services.config.current.relationships.enabled = False
    engine.interact(event("disabled", user="12345"))
    assert engine.get(event()) is None and engine.listing()["total"] == 1


def test_social_apis_auth_validation_conflict_edit_reset_and_delete(services):
    with TestClient(
        create_web_app(services.root, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        assert client.get("/api/emotion").status_code == 401
        assert client.get("/api/relationships").status_code == 401
        assert client.put("/api/emotion", json={"revision": 0, "values": {}}).status_code == 401
        client.post(
            "/api/auth/setup",
            json={"username": "social_admin", "password": "social-test-password-2026"},
        )
        state = client.get("/api/emotion").json()["current"]
        invalid = {"revision": state["revision"], "values": {"fatigue": 101}}
        assert client.put("/api/emotion", json=invalid).status_code == 422
        result = client.put(
            "/api/emotion", json={"revision": state["revision"], "values": {"fatigue": 88}}
        )
        assert result.status_code == 200 and result.json()["state"] == "tired"
        assert client.post("/api/emotion/reset", json={"revision": 0}).status_code == 409
        assert (
            client.post("/api/emotion/reset", json={"revision": result.json()["revision"]}).json()[
                "state"
            ]
            == "calm"
        )
        values = {
            "bot_id": "34567",
            "user_id": "23456",
            "display_name": "敏感称呼",
            "affinity": 88,
            "automatic": False,
        }
        assert (
            client.post("/api/relationships", json={**values, "affinity": 101}).status_code == 422
        )
        assert (
            client.post("/api/relationships", json={**values, "group_id": "bad"}).status_code == 422
        )
        row = client.post("/api/relationships", json=values).json()
        assert client.post("/api/relationships", json=values).status_code == 409
        assert client.get("/api/relationships", params={"query": "敏感"}).json()["total"] == 1
        assert client.get("/api/relationships", params={"query": "%"}).json()["total"] == 0
        edit = {"display_name": "新称呼", "affinity": -70, "automatic": True, "revision": 0}
        assert client.put(f"/api/relationships/{row['id']}", json=edit).status_code == 200
        assert client.put(f"/api/relationships/{row['id']}", json=edit).status_code == 409
        assert client.delete(f"/api/relationships/{row['id']}?revision=0").status_code == 409
        assert client.delete(f"/api/relationships/{row['id']}?revision=1").status_code == 200
        assert client.get("/api/relationships").json()["total"] == 0
        assert "敏感称呼" not in client.get("/api/audit").text
        assert "新称呼" not in client.get("/api/config/export").text


def test_config_rejects_invalid_thresholds_and_nonfinite_dimensions():
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {"emotion": {"high_threshold": 60, "low_threshold": 40, "hysteresis": 20}}
        )
    with pytest.raises(ValidationError):
        EmotionValues(fatigue=float("nan"))


@pytest.mark.parametrize(
    "reply,sends,expected", [("好的", True, 1), ("[[SILENT]]", True, 0), ("好的", False, 0)]
)
def test_agent_uses_current_scope_and_only_rewards_successful_reply(
    services, monkeypatch, reply, sends, expected
):
    captured = []
    settings = services.config.current
    settings.qq.enabled = True
    settings.behavior.reply_cooldown_seconds = 0
    settings.behavior.global_send_interval_seconds = 0
    services.relationships.create("34567", "23456", "12345", "群内称呼", 70, True)
    services.relationships.create("34567", "23456", "", "秘密私聊称呼", 90, True)
    monkeypatch.setattr(services.llm, "configured", lambda: True)

    async def completion(messages, **kwargs):
        captured.append(messages)
        return Completion(reply, None)

    def no_tools(*args, **kwargs):
        raise ProviderError("unsupported_capability", "fixture has no tools")

    async def sender(evt, payload):
        if not sends:
            raise RuntimeError("not delivered")

    monkeypatch.setattr(services.llm, "complete", completion)
    monkeypatch.setattr(services.llm, "chain", no_tools)
    services.agent.executor.sender = sender
    asyncio.run(services.agent.process([(event(group="12345"), "direct")]))
    assert "群内称呼" in captured[0][0]["content"]
    assert "秘密私聊称呼" not in str(captured)
    assert "全局模拟情绪" in captured[0][0]["content"]
    assert services.relationships.get(event(group="12345"))["affinity"] == 70 + expected
    assert len(services.emotion.history()) == expected
    settings.behavior.group_blocklist = ["12345"]
    asyncio.run(services.agent.process([(event("blocked", group="12345"), "direct")]))
    assert len(captured) == 1


def test_web_chat_uses_global_emotion_but_never_loads_qq_relationships(services, monkeypatch):
    services.relationships.create("34567", "23456", "", "不应公开称呼", 100, True)
    captured = []

    async def completion(messages, **kwargs):
        captured.append(messages)
        return Completion("测试回复", None)

    monkeypatch.setattr(services.llm, "complete", completion)
    with TestClient(
        create_web_app(services.root, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        client.post(
            "/api/auth/setup", json={"username": "chat_admin", "password": "social-chat-test-2026"}
        )
        assert client.post("/api/chat", json={"text": "你好"}).status_code == 200
    assert "全局模拟情绪" in captured[0][0]["content"]
    assert "不应公开称呼" not in str(captured)
    assert services.relationships.listing()["items"][0]["interactions"] == 0
