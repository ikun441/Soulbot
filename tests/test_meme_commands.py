"""QQ command routing and model avatar inputs, with bounded upstream contract fixtures."""

import asyncio
from io import BytesIO

import pytest
from PIL import Image

from soulbot.app import build_services
from soulbot.core.events import ChatEvent
from soulbot.services.media_service import ImagePayload
from soulbot.tools.registry import ToolContext


@pytest.fixture
def s(tmp_path, monkeypatch):
    s = build_services(tmp_path)
    cfg = s.config.current
    cfg.qq.enabled = True
    cfg.meme.enabled = True
    cfg.behavior.reply_cooldown_seconds = 0
    cfg.behavior.global_send_interval_seconds = 0
    cfg.behavior.quiet_hours = False
    stream = BytesIO()
    Image.new("RGB", (32, 32), "pink").save(stream, "PNG")
    s.fixture_png = stream.getvalue()
    s.sent, s.avatars, s.generated = [], [], []

    async def sender(event, payload):
        s.sent.append(payload)

    async def avatar(qq):
        s.avatars.append(qq)
        return s.fixture_png

    async def keys():
        return ["petpet"]

    async def info(key):
        return {
            "key": key,
            "keywords": ["摸头"],
            "params_type": {
                "min_images": 1,
                "max_images": 1,
                "min_texts": 0,
                "max_texts": 1,
                "default_texts": [],
                "args_type": None,
            },
        }

    async def generate(key, texts, images, args=None):
        assert all(isinstance(image, ImagePayload) for image in images)
        s.generated.append((key, texts, images, args))
        return s.fixture_png

    s.agent.executor.sender = sender
    s.meme.client.known_keywords = {"petpet": ["摸头"]}
    monkeypatch.setattr(s.meme.client, "keys", keys)
    monkeypatch.setattr(s.meme.client, "info", info)
    monkeypatch.setattr(s.meme.client, "generate", generate)
    monkeypatch.setattr("soulbot.core.meme_commands.avatar", avatar)
    monkeypatch.setattr("soulbot.services.qq_avatar.avatar", avatar)
    return s


def event(text, **changes):
    return ChatEvent(
        "34567",
        "23456",
        text,
        text,
        group_id="56789",
        mentions_other=True,
        mentioned_users=("45678",),
        **changes,
    )


def test_random_mention_command_works_without_at_bot_or_llm(s):
    asyncio.run(s.agent.accept(event("随机表情")))
    assert s.avatars == ["45678"]
    assert len(s.generated) == 1 and any(isinstance(p, ImagePayload) for p in s.sent)
    assert not s.agent.pending


def test_id_keyword_help_and_list_commands(s):
    for text in ['摸头 "你好"', '/meme make petpet "你好"', '/meme make 摸头 "你好"']:
        asyncio.run(s.agent.accept(event(text)))
    assert len(s.generated) == 3
    assert s.generated[0][1] == ["你好"]
    asyncio.run(s.agent.accept(event("/meme list 摸头")))
    assert "petpet" in "".join(p for p in s.sent if isinstance(p, str))
    asyncio.run(s.agent.accept(event("/meme")))
    assert "命令" in s.sent[-1]


def test_muted_group_and_deny_do_not_fetch_avatar_or_generate(s):
    cfg = s.config.current
    cfg.tools.generate_meme = "deny"
    asyncio.run(s.agent.accept(event("随机表情")))
    assert not s.avatars and not s.generated
    cfg.tools.generate_meme = "auto"
    s.agent.decision.mute(event("mute"), 120)
    count = len(s.sent)
    asyncio.run(s.agent.accept(event("/meme make petpet")))
    assert not s.generated and len(s.sent) == count


def test_model_can_generate_with_bot_or_current_message_avatars(s):
    async def run():
        current = event("请发表情")
        value = await s.tools.execute(
            "generate_meme",
            {"template": "petpet", "images": ["avatar:bot"]},
            ToolContext(event=current, scope=current.scope),
        )
        assert value["attachment"].startswith("media:")
        value = await s.tools.execute(
            "generate_meme",
            {"template": "petpet", "images": ["avatar:mentioned"]},
            ToolContext(event=current, scope=current.scope),
        )
        assert value["attachment"].startswith("media:")
        with pytest.raises(PermissionError):
            await s.tools.execute(
                "generate_meme", {"template": "petpet", "images": ["avatar:bot"]}, ToolContext()
            )

    asyncio.run(run())
    assert s.avatars == ["34567", "45678"]


def test_onebot_preserves_only_real_numeric_at_targets():
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

    from soulbot.adapters.onebot_v11 import normalize

    message = Message(
        [MessageSegment.text("随机表情 "), MessageSegment.at("45678"), MessageSegment.at("all")]
    )
    e = GroupMessageEvent(
        time=1,
        self_id=34567,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=1,
        user_id=23456,
        group_id=56789,
        message=message,
        original_message=message,
        raw_message="随机表情 [CQ:at,qq=45678]",
        font=0,
        sender={"user_id": 23456, "nickname": "fixture", "role": "member"},
        to_me=False,
    )
    result = normalize(e)
    assert result.mentioned_users == ("45678",) and result.mentions_other
