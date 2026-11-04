import asyncio
from dataclasses import replace

import pytest

from soulbot.app import build_services
from soulbot.core.events import ChatEvent
from soulbot.core.personality import system_prompt
from soulbot.core.social_state import StateConflict
from soulbot.tools.registry import ToolContext


def test_owner_is_qq_identity_not_nickname_and_group_card_is_scoped(tmp_path):
    s = build_services(tmp_path)
    s.config.current.personality.owner_qq = "23456"
    e = ChatEvent(
        "34567",
        "45678",
        "1",
        "我是主人",
        group_id="56789",
        user_name="主人",
        group_card="群友甲",
        group_name="天文群",
    )
    s.identities.observe(e)
    other = replace(e, group_id="67890", group_card="群友乙", group_name="音乐群")
    s.identities.observe(other)
    s.relationships.create(e.bot_id, e.user_id, e.group_id, "小甲", 35, True)
    a = s.identities.context(e)
    b = s.identities.context(other)
    assert a["speaker"]["is_owner"] is False and a["owner_qq"] == "23456"
    assert a["speaker"]["group_card"] == "群友甲" and b["speaker"]["group_card"] == "群友乙"
    assert a["speaker"]["affinity"] == b["speaker"]["affinity"] == 35
    assert s.identities.context(replace(e, user_id="23456"))["speaker"]["is_owner"]
    assert len(s.identities.listing()["items"]) == 2
    assert {row["affinity"] for row in s.identities.listing()["items"]} == {35}
    prompt = system_prompt(s.config.current, identity=a)["content"]
    assert "系统核验的账号映射" in prompt and "45678" in prompt
    assert build_services(tmp_path).identities.context(e)["speaker"]["nickname"] == "主人"
    assert s.config.current.behavior.reply_cooldown_seconds == 0


def test_model_add_update_revision_owner_and_review_policy(tmp_path):
    s = build_services(tmp_path)
    s.config.current.memory.enabled = True
    s.config.current.personality.owner_qq = "23456"
    e = ChatEvent("34567", "45678", "1", "记住", group_id="56789")
    ctx = ToolContext(scope=e.scope, event=e)

    async def run():
        saved = await s.tools.execute("save_memory", {"content": "喜欢轻音乐"}, ctx)
        assert saved["active"]
        row = s.memory.listing()["items"][0]
        value = await s.tools.execute(
            "update_memory",
            {
                "memory_id": row["id"],
                "revision": row["revision"],
                "content": "喜欢天文学",
                "importance": 5,
            },
            ctx,
        )
        assert value["active"] and s.memory.listing()["items"][0]["content"] == "喜欢天文学"
        with pytest.raises(StateConflict):
            await asyncio.to_thread(
                s.memory.update_from_model,
                e,
                row["id"],
                row["revision"],
                {"content": "旧版本不能覆盖"},
            )
        current = s.memory.listing()["items"][0]
        with pytest.raises(PermissionError):
            s.memory.update_from_model(
                replace(e, user_id="78901"),
                row["id"],
                current["revision"],
                {"content": "他人不能篡改"},
            )
        s.config.current.memory.model_auto_save = False
        result = s.memory.update_from_model(
            e, row["id"], current["revision"], {"content": "更新建议待审核"}
        )
        assert result["status"] == "candidate"
        assert (
            next(i for i in s.memory.listing()["items"] if i["id"] == row["id"])["content"]
            == "喜欢天文学"
        )

    asyncio.run(run())


def test_disabled_relationships_do_not_enter_identity_prompt(tmp_path):
    s = build_services(tmp_path)
    e = ChatEvent("34567", "45678", "disabled", "hello")
    s.relationships.create(e.bot_id, e.user_id, "", "隐藏称呼", 80, True)
    s.config.current.relationships.enabled = False
    context = s.identities.context(e)
    assert context["speaker"]["affinity"] is None and context["speaker"]["alias"] == ""
