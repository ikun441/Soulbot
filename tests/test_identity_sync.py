import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.core.social_state import StateConflict
from soulbot.services.identity_sync import SyncRequest


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    s.config.current.qq.enabled = True
    s.config.current.personality.owner_qq = "23456"
    s.calls = []

    async def friends(bot):
        s.calls.append("friends")
        return [{"user_id": 23456, "nickname": "主人昵称", "remark": "主人备注"}]

    async def groups(bot):
        s.calls.append("groups")
        return [
            {"group_id": 56789, "group_name": "天文群", "member_count": 2},
            {"group_id": 67890, "group_name": "音乐群", "member_count": 2},
        ]

    async def members(bot, group):
        s.calls.append(group)
        return [
            {
                "user_id": 23456,
                "nickname": "主人昵称",
                "card": "星星" if group == "56789" else "音符",
            },
            {"user_id": 45678, "nickname": "主人", "card": "其他成员"},
        ]

    s.qq_transport = SimpleNamespace(
        connected=lambda: ["34567"],
        directory_friends=friends,
        directory_groups=groups,
        directory_members=members,
    )
    return s


def request(**kwargs):
    return SyncRequest(**{"bot_id": "34567", **kwargs})


def event(user="23456", group="56789"):
    return ChatEvent("34567", user, "fixture", "hello", group_id=group)


async def finish(s, data=None):
    await s.identity_sync.begin(data or request())
    await s.identity_sync.task
    return s.identity_sync.status()


def test_full_sync_global_affinity_friendship_cards_and_owner_identity(s):
    s.relationships.create("34567", "23456", "", "全局主人", 75, True)
    status = asyncio.run(finish(s))
    assert status["status"] == "completed"
    assert status["friends"] == 1 and status["members"] == 4 and status["groups_done"] == 2
    a = s.identities.context(event())
    b = s.identities.context(event(group="67890"))
    assert a["speaker"]["affinity"] == b["speaker"]["affinity"] == 75
    assert a["speaker"]["group_card"] == "星星" and b["speaker"]["group_card"] == "音符"
    assert a["speaker"]["is_owner"] and a["speaker"]["is_friend"]
    assert not s.identities.context(event(user="45678"))["speaker"]["is_owner"]
    assert s.identities.context(event(user="45678"))["speaker"]["is_friend"] is None
    assert s.relationships.listing()["total"] == 1 and s.consent.listing()["total"] == 0
    assert s.identities.listing("主人备注")["total"] == 2
    assert len(s.identities.listing(offset=1, limit=1)["items"]) == 1


def test_blocked_groups_and_optional_member_sync_never_fetch_forbidden_members(s):
    s.config.current.behavior.group_blocklist = ["67890"]
    status = asyncio.run(finish(s))
    assert "67890" not in s.calls and status["skipped_groups"] == 1
    assert not s.identities.listing("67890")["items"]
    s.calls.clear()
    asyncio.run(finish(s, request(include_friends=False, include_members=False)))
    assert s.calls == ["groups"]


def test_partial_member_list_preserves_old_members_and_reports_incomplete(s):
    s.identities.observe(replace(event(user="78901"), user_name="旧成员", group_card="保留"))

    async def partial(bot, group):
        return [{"user_id": 23456, "nickname": "主人昵称", "card": "星星"}]

    s.qq_transport.directory_members = partial
    status = asyncio.run(finish(s))
    assert status["status"] == "partial" and status["failed_groups"] == 2
    assert s.identities.context(event(user="78901"))["speaker"]["group_card"] == "保留"


def test_one_failed_group_continues_other_groups_and_retains_prior_mapping(s):
    original = s.qq_transport.directory_members

    async def members(bot, group):
        if group == "56789":
            raise RuntimeError("private token must not appear")
        return await original(bot, group)

    s.qq_transport.directory_members = members
    status = asyncio.run(finish(s))
    assert status["status"] == "partial" and status["groups_done"] == 1
    assert "private token" not in json.dumps(status)


def test_limit_and_unjoined_group_are_explicit_and_cannot_trigger_arbitrary_lookup(s):
    status = asyncio.run(finish(s, request(max_groups=1)))
    assert status["status"] == "partial" and status["limited_groups"] == 1
    s.calls.clear()
    status = asyncio.run(finish(s, request(group_ids=["99999"])))
    assert status["status"] == "partial" and status["failed_groups"] == 1 and "99999" not in s.calls


def test_repeated_request_is_idempotent_and_conflicting_payload_is_rejected(s):
    data = request()

    async def run():
        await finish(s, data)
        before = len(s.calls)
        assert (await s.identity_sync.begin(data))["duplicate"] and len(s.calls) == before
        with pytest.raises(StateConflict):
            await s.identity_sync.begin(data.model_copy(update={"include_members": False}))

    asyncio.run(run())


def test_cancel_before_worker_first_turn_always_persists_terminal_state(s):
    async def run():
        for _ in range(10):
            await s.identity_sync.begin(request())
            status = await s.identity_sync.cancel()
            assert status["status"] == "cancelled"
        assert not s.calls

    asyncio.run(run())


def test_cancel_pending_platform_request_then_can_start_new_sync(s):
    original = s.qq_transport.directory_friends

    async def run():
        entered = asyncio.Event()

        async def blocked(bot):
            entered.set()
            await asyncio.Future()

        s.qq_transport.directory_friends = blocked
        await s.identity_sync.begin(request())
        await entered.wait()
        with pytest.raises(StateConflict):
            await s.identity_sync.begin(request())
        assert (await s.identity_sync.cancel())["status"] == "cancelled"
        assert not s.identities.listing()["items"]
        s.qq_transport.directory_friends = original
        assert (await finish(s))["status"] == "completed"

    asyncio.run(run())


def test_cancel_waits_for_thread_and_rolls_back_inflight_friend_batch(s, monkeypatch):
    entered, released = threading.Event(), threading.Event()
    original = s.identities.upsert_user

    def blocked(*args, **kwargs):
        entered.set()
        released.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(s.identities, "upsert_user", blocked)

    async def run():
        await s.identity_sync.begin(request())
        assert await asyncio.to_thread(entered.wait, 2)
        cancellation = asyncio.create_task(s.identity_sync.cancel())
        await asyncio.sleep(0.02)
        assert not cancellation.done()
        released.set()
        await cancellation

    asyncio.run(run())
    assert s.identity_sync.status()["status"] == "cancelled"
    assert not s.identities.listing()["items"]


def test_invalid_friend_data_does_not_clear_prior_friendship(s):
    s.identity_sync.friends("34567", [{"user_id": 23456, "nickname": "原名", "remark": "原备注"}])

    async def invalid(bot):
        return [{"user_id": "../secret", "nickname": "bad"}]

    s.qq_transport.directory_friends = invalid
    status = asyncio.run(finish(s))
    assert status["status"] == "failed"
    speaker = s.identities.context(event())["speaker"]
    assert speaker["is_friend"] and speaker["remark"] == "原备注"


def test_removed_friend_is_marked_with_time_and_keeps_historical_remark(s):
    s.identity_sync.friends("34567", [{"user_id": 23456, "nickname": "原名", "remark": "原备注"}])
    s.identity_sync.friends("34567", [])
    speaker = s.identities.context(event())["speaker"]
    assert (
        speaker["is_friend"] is False and speaker["friendship_checked_at"] and not speaker["remark"]
    )
    assert "原备注" in {row["name"] for row in s.identities.history("34567", "23456")["items"]}


def test_history_is_bounded_searchable_and_cards_remain_scoped(s):
    with s.db.connect() as db:
        for i in range(20):
            s.identities.upsert_user(db, "34567", "23456", f"昵称{i}", float(i + 1))
    history = s.identities.history("34567", "23456")["items"]
    assert len(history) == 8 and "昵称0" not in {row["name"] for row in history}
    assert s.identities.listing("昵称18")["total"] == 1
    s.identities.observe(replace(event(), group_card="天文名片"))
    s.identities.observe(replace(event(group="67890"), group_card="音乐名片"))
    assert "天文名片" not in str(
        s.identities.context(event(group="67890"))["speaker"]["known_names"]
    )


def test_startup_marks_interrupted_instead_of_replaying(s):
    s.db.set("identity_sync_state", json.dumps({"status": "running", "friends": 2}))
    asyncio.run(s.identity_sync.startup())
    assert s.identity_sync.status()["status"] == "interrupted" and not s.calls


def test_directory_and_history_apis_require_auth_conflicts_and_valid_ids(s):
    s.identities.observe(replace(event(), user_name="平台昵称"))
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as c:
        assert c.get("/api/identity-sync").status_code == 401
        assert (
            c.post("/api/identity-sync", json=request().model_dump(mode="json")).status_code == 401
        )
        c.post(
            "/api/auth/setup",
            json={"username": "directory_admin", "password": "identity-sync-fixture"},
        )
        assert c.get("/api/identities?limit=101").status_code == 422
        names = c.get("/api/identities/34567/23456/history").json()["items"]
        row = names[0]
        url = f"/api/identities/34567/23456/history/{row['id']}"
        assert c.delete(url, params={"last_seen": 0}).status_code == 409
        assert c.delete(url, params={"last_seen": row["last_seen"]}).status_code == 200
        assert s.identities.context(event())["speaker"]["nickname"] == "平台昵称"
        assert (
            c.post(
                "/api/identity-sync", json=request(bot_id="99999").model_dump(mode="json")
            ).status_code
            == 422
        )


def test_transport_uses_only_documented_readonly_napcat_calls(monkeypatch):
    import nonebot

    from soulbot.adapters.onebot_v11 import NoneBotTransport

    calls = []

    class Bot:
        async def call_api(self, action, **parameters):
            calls.append((action, parameters))
            return []

    monkeypatch.setattr(nonebot, "get_bot", lambda bot_id: Bot())

    async def run():
        t = NoneBotTransport()
        await t.directory_friends("34567")
        await t.directory_groups("34567")
        await t.directory_members("34567", "56789")

    asyncio.run(run())
    assert calls == [
        ("get_friend_list", {}),
        ("get_group_list", {}),
        ("get_group_member_list", {"group_id": 56789}),
    ]
