import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.core.scheduler import JobCreate, JobReview
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import Completion, ProviderError
from soulbot.services.diary_service import DiaryEdit, GenerateDiary
from soulbot.storage.database import Database


def stamp(day, hour=12, zone="Asia/Shanghai"):
    return (
        datetime.fromisoformat(f"{day}T{hour:02}:00:00").replace(tzinfo=ZoneInfo(zone)).timestamp()
    )


@pytest.fixture
def services(tmp_path):
    s = build_services(tmp_path)
    s.now = [stamp("2026-10-26")]
    s.diary.clock = s.scheduler.clock = s.consent.clock = lambda: s.now[0]
    s.config.current.diary.enabled = True
    s.config.current.diary.auto_generate = False
    return s


def event(message=None, group=None, user="23456", bot="34567"):
    return ChatEvent(
        bot,
        user,
        message or uuid4().hex,
        "private diary body must never be captured",
        group_id=group,
        directed=True,
    )


def facts(s, *, count=2, group=None, user="23456", bot="34567"):
    for i in range(count):
        s.now[0] += 1
        s.diary.record(event(group=group, user=user, bot=bot), has_text=True, has_media=i == 1)


def end_day(s):
    s.now[0] = stamp("2026-10-27", 1)


def diary(s):
    s.diary.generate("2026-10-26")
    return s.diary.listing()["items"][0]


def test_schema_ten_migration_preserves_data_and_is_idempotent(services):
    s = services
    s.db.set("preserved", "fixture")
    with s.db.connect() as db:
        for table in ("diary_events", "diary_preferences", "diaries", "diary_runs"):
            db.execute("DROP TABLE " + table)
        db.execute("UPDATE metadata SET value='10' WHERE key='schema_version'")
    db = Database(s.db.path)
    assert db.get("schema_version") == "11" and db.get("preserved") == "fixture"
    Database(db.path)
    facts(s)
    end_day(s)
    assert s.diary.generate("2026-10-26")["created"] == 1


def test_disabled_diary_does_not_capture_or_generate(services):
    s = services
    s.config.current.diary.enabled = False
    assert not s.diary.record(event())
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM diary_events").fetchone()[0] == 0
    with pytest.raises(ValueError):
        s.diary.generate("2026-10-26")


def test_event_capture_deduplicates_concurrently_without_message_text(services):
    s = services
    evt = event()
    with ThreadPoolExecutor(max_workers=4) as pool:
        captured = list(pool.map(lambda _: s.diary.record(evt), range(8)))
    assert sum(captured) == 1
    with s.db.connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM diary_events")]
    assert evt.text not in json.dumps(rows)
    assert "content" not in rows[0] and "text" not in rows[0]


def test_no_events_or_insufficient_scope_facts_never_invents_diary(services):
    s = services
    end_day(s)
    assert s.diary.generate("2026-10-26")["created"] == 0
    s.now[0] = stamp("2026-10-26")
    facts(s, count=1)
    facts(s, count=1, group="12345")
    end_day(s)
    result = s.diary.generate("2026-10-26")
    assert result["created"] == 0 and result["skipped_scopes"] == 2
    assert s.diary.listing()["total"] == 0


def test_diaries_are_isolated_by_bot_group_private_and_person(services):
    s = services
    for scope in ({}, {"group": "12345"}, {"group": "67890"}, {"user": "45678"}, {"bot": "87654"}):
        facts(s, **scope)
    end_day(s)
    assert s.diary.generate("2026-10-26")["created"] == 5
    rows = s.diary.listing()["items"]
    assert len({row["scope"] for row in rows}) == 5
    assert all(row["facts"]["total"] == 2 for row in rows)
    assert all(row["facts"]["text_chats"] == 2 and row["facts"]["media_chats"] == 1 for row in rows)
    assert "private diary body" not in json.dumps(rows)
    assert s.diary.listing(query="%")["total"] == 0


def test_day_bounds_use_timezone_and_dst_calendar_days(services):
    s = services
    start, end = s.diary.bounds("2026-11-01", "America/New_York")
    assert end - start == 25 * 3600
    s.now[0] = s.diary.bounds("2026-10-26")[0]
    s.diary.record(event())
    s.now[0] = s.diary.bounds("2026-10-26")[1] - 1
    s.diary.record(event())
    s.now[0] += 1
    s.diary.record(event())
    row = diary(s)
    assert row["facts"]["total"] == 2
    assert s.diary.status()["today_events"] == 1


def test_user_opt_out_stops_capture_and_generation_and_forget_pauses_future(services):
    s = services
    facts(s)
    facts(s, group="12345")
    s.diary.preference(event(), False)
    assert not s.diary.record(event())
    end_day(s)
    assert s.diary.status()["eligible_scopes"] == 1
    assert s.diary.generate("2026-10-26")["created"] == 1
    assert s.diary.listing()["items"][0]["group_id"] == "12345"
    s.diary.preference(event(), True)
    assert s.diary.generate("2026-10-26")["created"] == 1
    assert s.diary.forget(event()) == 1
    assert not s.diary.record(event())
    assert s.diary.listing()["total"] == 1


def test_concurrent_generation_is_idempotent_and_does_not_overwrite_edit(services):
    s = services
    facts(s)
    end_day(s)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: s.diary.generate("2026-10-26"), range(4)))
    assert sum(row["created"] for row in results) == 1
    row = s.diary.listing()["items"][0]
    edited = s.diary.edit(
        row["id"], DiaryEdit(revision=0, title="私人标题", role_text="管理员编写的角色表达")
    )
    s.diary.generate("2026-10-26")
    assert s.diary.get(row["id"])["role_text"] == edited["role_text"]
    assert s.diary.get(row["id"])["facts"] == row["facts"]
    with pytest.raises(StateConflict):
        s.diary.edit(row["id"], DiaryEdit(revision=0, title="stale", role_text="stale"))


def test_automatic_schedule_runs_once_after_time_and_persists_skip(services):
    s = services
    facts(s)
    s.config.current.diary.auto_generate = True
    s.now[0] = stamp("2026-10-27", 0) + 20 * 60
    asyncio.run(s.diary.tick())
    assert s.diary.listing()["total"] == 0
    s.now[0] += 11 * 60
    asyncio.run(s.diary.tick())
    assert s.diary.listing()["total"] == 1
    row = s.diary.listing()["items"][0]
    asyncio.run(s.diary.tick())
    assert s.diary.get(row["id"])["revision"] == 0
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM diary_runs").fetchone()[0] == 1
    s.now[0] = stamp("2026-10-28", 1)
    asyncio.run(s.diary.tick())
    assert s.diary.status()["last_auto_run"]["created_count"] == 0
    assert s.diary.status()["last_auto_run"]["day"] == "2026-10-27"


def test_retention_only_removes_old_events_not_generated_diaries(services):
    s = services
    facts(s)
    end_day(s)
    row = diary(s)
    s.config.current.diary.fact_retention_days = 7
    s.now[0] = stamp("2026-11-10")
    s.diary.status()
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM diary_events").fetchone()[0] == 0
    assert s.diary.get(row["id"])["facts"]["total"] == 2


def test_delete_removes_day_source_facts_without_touching_other_scope(services):
    s = services
    facts(s)
    facts(s, group="12345")
    end_day(s)
    s.diary.generate("2026-10-26")
    rows = s.diary.listing()["items"]
    private = next(row for row in rows if not row["group_id"])
    with pytest.raises(StateConflict):
        s.diary.delete(private["id"], 99)
    s.diary.delete(private["id"], private["revision"])
    with pytest.raises(LookupError):
        s.diary.get(private["id"])
    assert s.diary.generate("2026-10-26")["created"] == 0
    assert s.diary.listing()["total"] == 1


@pytest.mark.parametrize("value", ["2026-02-30", "not-a-date", "2026-1-1"])
def test_invalid_days_are_rejected(value):
    with pytest.raises(ValidationError):
        GenerateDiary(day=value)


def test_today_future_and_outside_retention_cannot_generate(services):
    s = services
    for day in ["2026-10-26", "2026-10-27", "2020-01-01"]:
        with pytest.raises(ValueError):
            s.diary.generate(day)


def test_diary_api_auth_immutable_facts_export_conflicts_and_no_private_audit(services):
    s = services
    facts(s)
    end_day(s)
    c = TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"})
    for path in ["/api/diaries", "/api/diaries/status", "/api/diaries/1/export"]:
        assert c.get(path).status_code == 401
    assert (
        c.post(
            "/api/auth/setup", json={"username": "diary_test", "password": "diary-test-2026!"}
        ).status_code
        == 200
    )
    assert c.post("/api/diaries/generate", json={"day": "2026-10-26"}).json()["created"] == 1
    row = c.get("/api/diaries").json()["items"][0]
    data = {"revision": 0, "title": "只属于本地的标题", "role_text": "我的私人角色文字"}
    assert (
        c.put(f"/api/diaries/{row['id']}", json={**data, "facts": {"total": 999}}).status_code
        == 422
    )
    updated = c.put(f"/api/diaries/{row['id']}", json=data).json()
    assert updated["facts"]["total"] == 2 and updated["role_source"] == "manual"
    assert c.put(f"/api/diaries/{row['id']}", json=data).status_code == 409
    exported = c.get(f"/api/diaries/{row['id']}/export")
    assert exported.status_code == 200 and exported.json()["role_text"] == data["role_text"]
    assert (
        "attachment" in exported.headers["content-disposition"]
        and exported.headers["cache-control"] == "no-store"
    )
    assert (
        data["title"] not in c.get("/api/audit").text
        and data["role_text"] not in c.get("/api/config/export").text
    )
    assert c.post("/api/diaries/1/send", json={}).status_code == 404
    assert c.delete(f"/api/diaries/{row['id']}?revision=0").status_code == 409
    assert c.delete(f"/api/diaries/{row['id']}?revision=1").status_code == 200


@pytest.mark.parametrize(
    "reply,success,expected", [("你好", True, 1), ("[[SILENT]]", True, 0), ("你好", False, 0)]
)
def test_only_confirmed_agent_deliveries_become_facts(
    services, monkeypatch, reply, success, expected
):
    s = services
    s.config.current.qq.enabled = True
    s.config.current.behavior.reply_cooldown_seconds = 0
    monkeypatch.setattr(s.llm, "configured", lambda: True)

    async def complete(*args, **kwargs):
        return Completion(reply, None)

    def no_tools(*args, **kwargs):
        raise ProviderError("unsupported_capability", "fixture")

    async def send(evt, payload):
        if not success:
            raise RuntimeError("unconfirmed platform send")

    monkeypatch.setattr(s.llm, "complete", complete)
    monkeypatch.setattr(s.llm, "chain", no_tools)
    s.agent.executor.sender = send
    asyncio.run(s.agent.process([(event(), "direct")]))
    assert s.diary.status()["today_events"] == expected


def test_diary_record_failure_does_not_break_chat_delivery(services, monkeypatch):
    s = services
    s.config.current.qq.enabled = True
    s.config.current.behavior.reply_cooldown_seconds = 0
    monkeypatch.setattr(s.llm, "configured", lambda: True)

    async def complete(*args, **kwargs):
        return Completion("真实回复", None)

    def no_tools(*args, **kwargs):
        raise ProviderError("unsupported_capability", "fixture")

    async def send(evt, payload):
        pass

    def fail(*args, **kwargs):
        raise RuntimeError("fixture storage failure")

    monkeypatch.setattr(s.llm, "complete", complete)
    monkeypatch.setattr(s.llm, "chain", no_tools)
    monkeypatch.setattr(s.diary, "record", fail)
    s.agent.executor.sender = send
    evt = event()
    asyncio.run(s.agent.process([(evt, "direct")]))
    assert s.agent.conversation.history(evt.scope, 1)[-1]["content"] == "真实回复"


def test_only_confirmed_proactive_job_adds_one_fact(services):
    s = services
    s.config.current.qq.enabled = True
    s.config.current.proactive.enabled = True
    s.config.current.behavior.global_send_interval_seconds = 0.1
    s.qq_transport = SimpleNamespace(connected=lambda: ["34567"])
    s.consent.set_user(event(), consent=True)

    async def send(evt, payload):
        pass

    s.agent.executor.sender = send
    data = JobCreate(
        request_id=uuid4(),
        bot_id="34567",
        user_id="23456",
        content="never copied into diary",
        earliest_at=s.now[0] - 300,
        latest_at=s.now[0] + 3600,
    )
    row = s.scheduler.create(data)["item"]
    s.scheduler.review(row["id"], JobReview(revision=0, action="approve"))
    asyncio.run(s.scheduler.tick())
    asyncio.run(s.scheduler.tick())
    assert s.diary.status()["today_events"] == 1
    with s.db.connect() as db:
        assert db.execute("SELECT kind FROM diary_events").fetchone()[0] == "proactive"
        assert "never copied" not in str(
            [dict(row) for row in db.execute("SELECT * FROM diary_events")]
        )


def test_user_diary_commands_delete_during_cooldown_and_disable_collection(services, monkeypatch):
    s = services
    s.config.current.qq.enabled = True
    s.config.current.behavior.reply_cooldown_seconds = 600
    facts(s)
    end_day(s)
    diary(s)

    async def send(evt, payload):
        pass

    async def forbidden(*args, **kwargs):
        raise AssertionError("Diary controls must not call LLM")

    s.agent.executor.sender = send
    monkeypatch.setattr(s.llm, "complete", forbidden)

    async def check():
        await s.agent.accept(replace(event(), text="/日记 关闭"))
        await s.agent.accept(replace(event(), text="/删除日记"))

    asyncio.run(check())
    assert s.diary.listing()["total"] == 0 and not s.diary.record(event())


def test_worker_start_and_stop_are_idempotent(services):
    s = services

    async def check():
        await s.diary.startup()
        task = s.diary.task
        await s.diary.startup()
        assert s.diary.task is task
        await s.diary.close()
        await s.diary.close()
        assert s.diary.task is None

    asyncio.run(check())


def test_deleted_diary_id_cannot_target_another_persons_new_entry(services):
    s = services
    facts(s)
    end_day(s)
    old = diary(s)
    s.diary.delete(old["id"], old["revision"])
    s.now[0] = stamp("2026-10-26", 13)
    facts(s, user="45678")
    end_day(s)
    new = diary(s)
    assert new["id"] > old["id"] and new["user_id"] == "45678"
    with pytest.raises(LookupError):
        s.diary.delete(old["id"], old["revision"])
    assert s.diary.get(new["id"])["facts"]["total"] == 2
