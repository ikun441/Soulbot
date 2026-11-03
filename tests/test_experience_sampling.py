import asyncio
import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from soulbot.app import build_services
from soulbot.core.events import ChatEvent
from soulbot.core.memory import MemoryCreate, MemoryEdit
from soulbot.providers.llm.base import Completion
from soulbot.services.diary_periods import PolishDiary
from soulbot.services.qzone_service import DraftCreate


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    now = datetime(2026, 11, 3, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    for component in (s.diary, s.memory, s.experiences, s.qzone, s.agent.conversation):
        component.clock = lambda: now
    s.config.current.memory.enabled = True
    s.config.current.diary.enabled = True
    s.config.current.diary.min_events = 1
    s.config.current.qzone.enabled = True
    s.config.current.personality.owner_qq = "23456"
    s.config.current.qq.enabled = True
    return s


def add_context(s, text, group=None, user="23456"):
    event = ChatEvent("34567", user, uuid4().hex, text, group_id=group, directed=True)
    s.agent.conversation.remember(event.scope, text, "这是一次有回执的回复", s.config.current)
    s.diary.record(event)
    return event


def test_sampling_uses_actual_timestamps_shared_memory_and_source_preferences(s):
    a = add_context(s, "我最近想研究天文学和望远镜")
    b = add_context(s, "群里聊起了喜欢的音乐作品", group="56789", user="45678")
    row = s.memory.create(
        MemoryCreate(bot_id=a.bot_id, user_id=a.user_id, content="以前喜欢轻音乐")
    )["item"]
    s.memory.edit(
        row["id"],
        MemoryEdit(
            content=row["content"],
            revision=row["revision"],
            status="approved",
            long_term_allowed=True,
        ),
    )
    with s.db.connect() as db:
        db.execute(
            "INSERT INTO chat_context(scope,role,content,expires) VALUES (?,'user','没有可核验时间的旧内容',?)",
            (a.scope, s.diary.clock() + 86400),
        )
    samples = s.experiences.sample(day="2026-11-03")
    assert len(samples["memories"]) == 1
    assert {r["content"] for r in samples["chats"]} == {a.text, b.text}
    assert not s.experiences.sample(day="2026-11-02")["chats"]
    s.memory.preference(b, False)
    assert [r["content"] for r in s.experiences.sample(day="2026-11-03")["chats"]] == [a.text]
    s.agent.conversation.clear()
    assert not s.experiences.sample()["chats"]


def test_diary_expression_contains_real_sample_pack_not_only_counts(s, monkeypatch):
    add_context(s, "我今天认真聊了天文学和星空")
    s.diary.generate("2026-11-03", include_current_day=True)
    item = s.diary.listing()["items"][0]
    calls = []

    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return Completion("今天聊到了星空，也想起以前偏爱的宁静话题。", None)

    monkeypatch.setattr(s.llm, "complete", complete)
    asyncio.run(
        s.periods.polish(item["id"], PolishDiary(request_id=uuid4(), revision=item["revision"]))
    )
    payload = json.loads(calls[0][0][1]["content"])
    assert payload["samples"]["chats"][0]["content"] == "我今天认真聊了天文学和星空"
    assert payload["facts"]["chat_replies"] == 1
    assert calls[0][1]["include_memory"] is False
    assert s.diary.get(item["id"])["role_source"] == "model"


def test_qzone_uses_samples_but_rejects_literal_chat_disclosure(s, monkeypatch):
    event = add_context(s, "这条来自私人聊天的完整叙述不应原样发布到公共空间")
    calls = []

    async def complete(messages, **kwargs):
        calls.append(messages)
        return Completion("把对星空的好奇，留给今夜的一点想象。", None)

    monkeypatch.setattr(s.llm, "complete", complete)
    data = DraftCreate(request_id=uuid4(), bot_id=event.bot_id, content="公开的星空随想", mode="ai")
    result = asyncio.run(s.qzone.create(data))
    assert result["status"] == "pending_approval"
    assert json.loads(calls[0][1]["content"])["samples"]["chats"]
    assert s.experiences.public_echo(event.text)
    assert s.qzone.content_issues(event.text)
