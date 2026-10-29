import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.adapters.qzone import probe_qzone
from soulbot.app import build_services, create_web_app
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import Completion, ProviderError
from soulbot.services.config_service import Settings
from soulbot.services.qzone_service import DraftCreate, DraftEdit, DraftPublish, DraftReview
from soulbot.storage.database import Database


class Transport:
    """Explicit local test fixture; never connects to or publishes on QQ."""

    def __init__(self):
        self.sent = []
        self.supported = True
        self.result = {"tid": "fixture_tid_1"}
        self.after_probe = None
        self.before_send = None

    def connected(self):
        return ["34567", "45678"]

    async def qzone_capability(self, bot_id):
        if self.after_probe:
            self.after_probe()
        return {
            "supported": self.supported,
            "connection": "fixture" if self.supported else None,
            "state": "verified_contract",
            "message": "Explicit test fixture",
        }

    async def qzone_publish(self, bot, content, ugc, connection, guard):
        if self.before_send:
            self.before_send()
        if not guard():
            raise ValueError("Guard rejected")
        self.sent.append((bot, content, ugc, connection))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def s(tmp_path):
    services = build_services(tmp_path)
    services.now = [1793066400.0]
    services.qzone.clock = lambda: services.now[0]
    services.config.current.personality.owner_qq = "23456"
    services.config.current.qq.enabled = True
    services.qq_transport = Transport()
    return services


def create(s, **kwargs):
    return asyncio.run(
        s.qzone.create(
            DraftCreate(
                request_id=uuid4(),
                bot_id="34567",
                content="今天想分享一点春日的轻松心情。",
                **kwargs,
            )
        )
    )


def approve(s, row):
    return s.qzone.review(
        row["id"], DraftReview(revision=row["revision"], action="approve", public_confirmed=True)
    )


def publish(s, row):
    return asyncio.run(s.qzone.publish(row["id"], DraftPublish(revision=row["revision"])))


def ready(s):
    s.config.current.qzone.enabled = True
    return approve(s, create(s))


def test_migration_from_eleven_is_idempotent_and_preserves_existing_data(s):
    s.db.set("preserved", "fixture")
    with s.db.connect() as db:
        for table in ("qzone_drafts", "qzone_attempts", "qzone_events"):
            db.execute("DROP TABLE " + table)
        db.execute("UPDATE metadata SET value='11' WHERE key='schema_version'")
    again = Database(s.db.path)
    assert again.get("schema_version") == "15" and again.get("preserved") == "fixture"
    Database(s.db.path)
    assert create(s)["status"] == "pending_approval"


def test_default_disabled_and_no_implicit_send(s):
    row = approve(s, create(s))
    assert not Settings().qzone.enabled
    with pytest.raises(StateConflict):
        publish(s, row)
    assert not s.qq_transport.sent
    assert s.qzone.status()["used_last_seven_days"] == 0


@pytest.mark.parametrize("value", [{"min_interval_hours": 71}, {"max_posts_per_week": 3}])
def test_config_cannot_weaken_baseline(value):
    with pytest.raises(ValidationError):
        Settings.model_validate({"qzone": value})


def test_create_is_concurrently_idempotent_and_request_reuse_conflicts(s):
    data = DraftCreate(request_id=uuid4(), bot_id="34567", content="公开主题")
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(lambda _: asyncio.run(s.qzone.create(data)), range(8)))
    assert len({r["id"] for r in items}) == 1
    with pytest.raises(StateConflict):
        asyncio.run(s.qzone.create(data.model_copy(update={"content": "其他主题"})))
    assert s.qzone.listing()["total"] == 1


@pytest.mark.parametrize(
    "content",
    [
        "联系 QQ 12345678",
        "a@example.com",
        "api_key=secret",
        "私聊记录摘录",
        "https://localhost/admin",
        "https://example.org/?token=secret",
        "password: secret",
    ],
)
def test_privacy_flag_blocks_approval_and_private_sources_cannot_be_injected(s, content):
    row = create(s)
    row = s.qzone.edit(
        row["id"], DraftEdit(revision=row["revision"], content=content, audience="friends")
    )
    assert row["privacy_issues"]
    with pytest.raises(ValueError):
        approve(s, row)
    with pytest.raises(ValidationError):
        DraftCreate(
            request_id=uuid4(), bot_id="34567", content="文字", source="private_memory", memory_id=1
        )
    assert not s.qq_transport.sent


def test_approval_requires_manual_attestation_and_owner(s):
    row = create(s)
    with pytest.raises(ValueError):
        s.qzone.review(row["id"], DraftReview(revision=0, action="approve"))
    s.config.current.personality.owner_qq = ""
    with pytest.raises(ValueError):
        approve(s, row)


def test_edit_invalidates_approval_and_stale_revision(s):
    row = ready(s)
    edited = s.qzone.edit(
        row["id"], DraftEdit(revision=row["revision"], content="修改后的公开内容", audience="self")
    )
    assert edited["status"] == "pending_approval" and edited["approved_at"] is None
    with pytest.raises(StateConflict):
        publish(s, row)
    with pytest.raises(StateConflict):
        s.qzone.edit(
            row["id"], DraftEdit(revision=row["revision"], content="旧窗口", audience="friends")
        )
    assert not s.qq_transport.sent
    confirmed = publish(s, approve(s, edited))
    assert confirmed["status"] == "published"
    assert s.qq_transport.sent[0][1:3] == ("修改后的公开内容", 64)


@pytest.mark.parametrize("change", ["owner", "qq", "expired"])
def test_owner_change_qq_disable_and_expired_approval_prevent_send(s, change):
    row = ready(s)
    if change == "owner":
        s.config.current.personality.owner_qq = "45678"
    elif change == "qq":
        s.config.current.qq.enabled = False
    else:
        s.now[0] += 86400
    with pytest.raises(StateConflict):
        publish(s, row)
    assert not s.qq_transport.sent


def test_unsupported_or_disconnected_retains_approved_draft_without_quota(s):
    row = ready(s)
    s.qq_transport.supported = False
    with pytest.raises(ValueError):
        publish(s, row)
    assert s.qzone.get(row["id"])["status"] == "approved"
    assert s.qzone.status()["used_last_seven_days"] == 0
    s.qq_transport = None
    assert asyncio.run(s.qzone.capability("34567"))["state"] == "disconnected"


def test_revoke_during_capability_probe_prevents_publish(s):
    row = ready(s)
    s.qq_transport.after_probe = lambda: s.qzone.review(
        row["id"], DraftReview(revision=row["revision"], action="cancel")
    )
    with pytest.raises(StateConflict):
        publish(s, row)
    assert not s.qq_transport.sent
    assert s.qzone.status()["used_last_seven_days"] == 0


def test_disable_at_transport_boundary_blocks_send_and_reserves_conservatively(s):
    row = ready(s)
    s.qq_transport.before_send = lambda: setattr(s.config.current.qzone, "enabled", False)
    assert publish(s, row)["status"] == "uncertain"
    assert not s.qq_transport.sent
    assert s.qzone.status()["used_last_seven_days"] == 1


def test_simultaneous_drafts_share_atomic_global_quota(s):
    a = ready(s)
    b = approve(
        s,
        asyncio.run(
            s.qzone.create(
                DraftCreate(request_id=uuid4(), bot_id="45678", content="另一个账号的公开文字")
            )
        ),
    )

    def attempt(row):
        try:
            return publish(s, row)["status"]
        except StateConflict:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, [a, b]))
    assert sorted(result) == ["blocked", "published"]
    assert len(s.qq_transport.sent) == 1


def test_same_draft_can_only_dispatch_once_under_race(s):
    row = ready(s)

    def attempt(_):
        try:
            return publish(s, row)["status"]
        except StateConflict:
            return "blocked"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results.count("published") == 1 and len(s.qq_transport.sent) == 1


def test_72_hour_and_rolling_week_quota_survive_restart_timezone_and_other_bot(s):
    first = ready(s)
    publish(s, first)
    for elapsed, allowed in [
        (72 * 3600 - 1, False),
        (1, True),
        (72 * 3600, False),
        (24 * 3600, True),
    ]:
        s.now[0] += elapsed
        # New service reads existing attempts; timezone doesn't affect rolling windows.
        from soulbot.services.qzone_service import QZoneService

        s.qzone = QZoneService(s, clock=lambda: s.now[0])
        s.config.current.soulbot.timezone = "UTC"
        row = approve(s, create(s))
        if allowed:
            assert publish(s, row)["status"] == "published"
        else:
            with pytest.raises(StateConflict):
                publish(s, row)
    assert len(s.qq_transport.sent) == 3


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"tid": ""},
        {"tid": 123},
        {"tid": "<unsafe>"},
        RuntimeError("upstream secret body"),
        asyncio.TimeoutError(),
    ],
)
def test_invalid_receipt_and_upstream_errors_are_uncertain_and_never_retried(s, result):
    row = ready(s)
    s.qq_transport.result = result
    item = publish(s, row)
    assert item["status"] == "uncertain" and item["tid"] is None
    assert "upstream secret body" not in json.dumps(item)
    s.now[0] += 8 * 86400
    with pytest.raises(StateConflict):
        publish(s, item)
    assert len(s.qq_transport.sent) == 1
    with s.db.connect() as db:
        assert db.execute("SELECT status FROM qzone_attempts").fetchone()[0] == "uncertain"
        assert "secret body" not in str([dict(r) for r in db.execute("SELECT * FROM audit")])


def test_cancellation_is_terminal_and_quota_persists(s):
    row = ready(s)
    s.qq_transport.result = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        publish(s, row)
    assert s.qzone.get(row["id"])["status"] == "uncertain"
    assert s.qzone.status()["used_last_seven_days"] == 1


def test_interrupted_process_recovers_without_resend(s):
    row = ready(s)
    with s.db.connect() as db:
        db.execute(
            "INSERT INTO qzone_attempts VALUES (?,?,?,'reserved')",
            (row["id"], row["bot_id"], s.now[0]),
        )
        db.execute(
            "UPDATE qzone_drafts SET status='publishing',lease_until=? WHERE id=?",
            (s.now[0] + 90, row["id"]),
        )
    s.now[0] += 91
    assert s.qzone.get(row["id"])["status"] == "uncertain"
    assert s.qzone.status()["used_last_seven_days"] == 1
    assert not s.qq_transport.sent


def test_ai_generation_only_sees_public_topic_no_history_or_tools_and_deduplicates(s, monkeypatch):
    calls = []
    s.config.current.personality.prompt = "private-persona-secret"

    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return Completion("公开主题的轻松表达", None)

    monkeypatch.setattr(s.llm, "complete", complete)
    data = DraftCreate(request_id=uuid4(), bot_id="34567", content="春日和花朵", mode="ai")
    row = asyncio.run(s.qzone.create(data))
    assert row["status"] == "pending_approval" and row["source"] == "ai_public_topic"
    assert asyncio.run(s.qzone.create(data))["id"] == row["id"]
    assert len(calls) == 1 and calls[0][0][1]["content"] == "春日和花朵"
    assert "private-persona-secret" not in json.dumps(calls)
    assert calls[0][1] == {"purpose": "qzone_draft", "output_limit": 512}
    with pytest.raises(ValueError):
        asyncio.run(s.qzone.create(data.model_copy(update={"request_id": uuid4()})))
    assert not s.qq_transport.sent


def test_ai_private_topic_rejected_and_private_output_needs_edit(s, monkeypatch):
    async def complete(*args, **kwargs):
        return Completion("联系 QQ 12345678", None)

    monkeypatch.setattr(s.llm, "complete", complete)
    with pytest.raises(ValueError):
        asyncio.run(
            s.qzone.create(
                DraftCreate(request_id=uuid4(), bot_id="34567", content="私聊记录", mode="ai")
            )
        )
    row = create(s, mode="ai")
    assert row["privacy_issues"]
    with pytest.raises(ValueError):
        approve(s, row)


def test_ai_failure_is_not_retried_by_repeated_request(s, monkeypatch):
    calls = []

    async def complete(*args, **kwargs):
        calls.append(1)
        raise ProviderError("unavailable", "服务不可用")

    monkeypatch.setattr(s.llm, "complete", complete)
    data = DraftCreate(request_id=uuid4(), bot_id="34567", content="公开主题", mode="ai")
    with pytest.raises(ProviderError):
        asyncio.run(s.qzone.create(data))
    assert asyncio.run(s.qzone.create(data))["status"] == "generation_failed"
    assert len(calls) == 1


class Bot:
    def __init__(self, version="4.18.38", name="NapCat.Onebot", user="34567", online=True):
        self.calls = []
        self.responses = {
            "get_version_info": {"app_name": name, "app_version": version},
            "get_status": {"online": online, "good": True},
            "get_login_info": {"user_id": user},
        }

    async def call_api(self, action, **params):
        self.calls.append((action, params))
        return self.responses[action]


@pytest.mark.parametrize(
    "params,supported",
    [
        ({}, True),
        ({"version": "99.0.0"}, False),
        ({"name": "other"}, False),
        ({"user": "45678"}, False),
        ({"online": False}, False),
    ],
)
def test_capability_probe_only_uses_readonly_calls_and_exact_verified_version(params, supported):
    bot = Bot(**params)
    result = asyncio.run(probe_qzone(bot, "34567"))
    assert result["supported"] is supported
    assert [c[0] for c in bot.calls] == ["get_version_info", "get_status", "get_login_info"]
    assert "nickname" not in result


def test_native_adapter_maps_exact_contract_and_rejects_reconnected_bot(monkeypatch):
    from soulbot.adapters.onebot_v11 import NoneBotTransport

    bot = Bot()
    bot.responses["send_qzone_msg"] = {"tid": "fixture"}
    monkeypatch.setattr("nonebot.get_bot", lambda _: bot)
    transport = NoneBotTransport()
    with pytest.raises(ValueError):
        asyncio.run(transport.qzone_publish("34567", "文字", 4, "wrong", lambda: True))
    with pytest.raises(ValueError):
        asyncio.run(transport.qzone_publish("34567", "文字", 4, bot, lambda: False))
    result = asyncio.run(transport.qzone_publish("34567", "文字", 4, bot, lambda: True))
    assert result["tid"] == "fixture"
    assert bot.calls == [("send_qzone_msg", {"content": "文字", "images": [], "ugc_right": 4})]


def test_api_auth_csrf_schema_privacy_and_full_local_flow(s):
    app = create_web_app(s.root, s)
    headers = {"X-Soulbot-Request": "1"}
    with TestClient(app) as c:
        assert c.get("/api/qzone/drafts").status_code == 401
        assert (
            c.post(
                "/api/auth/setup",
                headers=headers,
                json={"username": "qzone_test", "password": "qzone-test-2026!"},
            ).status_code
            == 200
        )
        body = {
            "request_id": str(uuid4()),
            "bot_id": "34567",
            "content": "公开内容",
            "audience": "friends",
        }
        assert c.post("/api/qzone/drafts", json=body).status_code == 403
        assert (
            c.post(
                "/api/qzone/drafts", headers={**headers, "Origin": "http://evil.example"}, json=body
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/api/qzone/drafts", headers=headers, json={**body, "source": "diary"}
            ).status_code
            == 422
        )
        row = c.post("/api/qzone/drafts", headers=headers, json=body).json()
        assert "input_hash" not in row
        assert (
            c.post(
                f"/api/qzone/drafts/{row['id']}/publish", headers=headers, json={"revision": 0}
            ).status_code
            == 409
        )
        approved = c.post(
            f"/api/qzone/drafts/{row['id']}/review",
            headers=headers,
            json={"revision": 0, "action": "approve", "public_confirmed": True},
        ).json()
        s.config.current.qzone.enabled = True
        cap = c.post("/api/qzone/capability?bot_id=34567", headers=headers).json()
        assert "connection" not in cap
        result = c.post(
            f"/api/qzone/drafts/{row['id']}/publish",
            headers=headers,
            json={"revision": approved["revision"]},
        )
        assert result.status_code == 200 and result.json()["status"] == "published"
        assert c.get("/api/qzone/drafts?limit=0").status_code == 422
        assert c.get("/api/qzone/drafts/99999").status_code == 404
        assert c.get("/api/qzone/status").json()["used_last_seven_days"] == 1
    assert len(s.qq_transport.sent) == 1


def test_permission_engine_requires_trusted_approval_and_missing_policy_still_denies(s):
    from soulbot.core.permissions import Permission, PermissionEngine

    engine = PermissionEngine({"publish_qzone": Permission.APPROVAL})
    with pytest.raises(PermissionError):
        engine.require("publish_qzone")
    with pytest.raises(PermissionError):
        engine.require("publish_qzone", approved="true")
    with pytest.raises(PermissionError):
        engine.require("unregistered", approved=True)
    engine.require("publish_qzone", approved=True)
    row = ready(s)
    s.qzone.permissions.policies["publish_qzone"] = Permission.DENY
    with pytest.raises(StateConflict):
        publish(s, row)
    assert not s.qq_transport.sent


def test_even_auto_permission_cannot_override_mandatory_review_and_feature_switch(s):
    from soulbot.core.permissions import Permission

    row = ready(s)
    s.qzone.permissions.policies["publish_qzone"] = Permission.AUTO
    s.config.current.qzone.enabled = False
    with pytest.raises(StateConflict):
        publish(s, row)
    s.config.current.qzone.enabled = True
    row = s.qzone.edit(
        row["id"], DraftEdit(revision=row["revision"], content="新的公开文字", audience="friends")
    )
    with pytest.raises(StateConflict):
        publish(s, row)
    assert not s.qq_transport.sent


def auto(s):
    s.config.current.qzone.enabled = True
    s.config.current.qzone.require_approval = False
    s.config.current.qzone.autonomous_enabled = True
    s.config.current.qzone.auto_bot_id = "34567"
    s.config.current.qzone.auto_audience = "self"
    s.config.current.qzone.auto_topics = ["阳光与猫咪"]


def test_opt_out_approval_config_is_explicit_and_auto_needs_target():
    assert not Settings.model_validate(
        {"qzone": {"require_approval": False}}
    ).qzone.require_approval
    with pytest.raises(ValidationError):
        Settings.model_validate({"qzone": {"autonomous_enabled": True}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"qzone": {"autonomous_enabled": True, "require_approval": False}})


def test_no_approval_mode_dispatches_without_forging_human_review(s):
    auto(s)
    row = create(s)
    assert row["status_label"] == "可直接发布"
    item = publish(s, row)
    assert (
        item["status"] == "published"
        and item["approved_at"] is None
        and item["approved_owner"] is None
    )
    assert any(e["reason"] == "auto_policy" for e in item["events"])


def test_no_approval_still_blocks_privacy_and_permission_deny(s):
    auto(s)
    row = create(s)
    row = s.qzone.edit(row["id"], DraftEdit(revision=0, content="QQ 12345678", audience="self"))
    with pytest.raises(StateConflict):
        publish(s, row)
    assert not s.qq_transport.sent


def test_autonomous_tick_generates_and_publishes_without_human_fields(s, monkeypatch):
    auto(s)
    calls = []

    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return Completion("愿好心情像阳光一样温暖喵～", None)

    monkeypatch.setattr(s.llm, "complete", complete)

    async def run():
        await asyncio.gather(s.qzone.tick(), s.qzone.tick())
        await s.qzone.tick()

    asyncio.run(run())
    assert len(s.qq_transport.sent) == 1 and len(calls) == 1
    row = s.qzone.listing()["items"][0]
    assert row["status"] == "published" and row["source"] == "autonomous_public_topic"
    assert row["approved_at"] is None
    assert calls[0][1]["purpose"] == "qzone_autonomous"
    assert s.qq_transport.sent[0][2] == 64
    assert s.qzone.status()["autonomous_state"]["reason"] == "published"


@pytest.mark.parametrize(
    "condition", ["disabled", "approval", "qq", "offline", "quiet", "private_topic", "unsupported"]
)
def test_autonomous_guards_do_not_generate_or_send(s, monkeypatch, condition):
    auto(s)
    if condition == "disabled":
        s.config.current.qzone.autonomous_enabled = False
    elif condition == "approval":
        s.config.current.qzone.require_approval = True
    elif condition == "qq":
        s.config.current.qq.enabled = False
    elif condition == "offline":
        s.config.current.qzone.auto_bot_id = "99999"
    elif condition == "quiet":
        s.now[0] = datetime(2026, 10, 27, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    elif condition == "private_topic":
        s.config.current.qzone.auto_topics = ["私聊记录"]
    elif condition == "unsupported":
        s.qq_transport.supported = False

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not generate")

    monkeypatch.setattr(s.llm, "complete", forbidden)
    asyncio.run(s.qzone.tick())
    assert not s.qq_transport.sent
    assert not s.qzone.listing()["items"]


def test_autonomous_policy_revocation_during_generation_retains_draft(s, monkeypatch):
    auto(s)

    async def complete(*args, **kwargs):
        s.config.current.qzone.autonomous_enabled = False
        return Completion("一个公开主题表达", None)

    monkeypatch.setattr(s.llm, "complete", complete)
    asyncio.run(s.qzone.tick())
    assert not s.qq_transport.sent
    assert s.qzone.listing()["items"][0]["status"] == "pending_approval"
    assert s.qzone.status()["autonomous_state"]["reason"] == "policy_changed"


def test_autonomous_unknown_cost_defers_persistently_without_repeated_generation(s, monkeypatch):
    auto(s)
    calls = []

    async def complete(*args, **kwargs):
        calls.append(1)
        raise ProviderError("unknown_cost", "价格未知")

    monkeypatch.setattr(s.llm, "complete", complete)
    asyncio.run(s.qzone.tick())
    from soulbot.services.qzone_service import QZoneService

    s.qzone = QZoneService(s, clock=lambda: s.now[0])
    asyncio.run(s.qzone.tick())
    assert calls == [1] and not s.qq_transport.sent
    assert s.qzone.status()["autonomous_state"]["reason"] == "unknown_cost"


def test_autonomous_lease_not_released_by_previous_owner(s):
    s.qzone.auto_state(request_id="new", lease_until=s.now[0] + 180)
    s.qzone.auto_state(lease_id="old", lease_until=0)
    assert s.qzone.status()["autonomous_state"]["lease_until"] == s.now[0] + 180


def test_autonomous_lifecycle_is_idempotent_and_stops(s):
    auto(s)
    s.config.current.qzone.enabled = False

    async def run():
        await s.qzone.startup()
        task = s.qzone.task
        await s.qzone.startup()
        assert s.qzone.task is task
        await s.qzone.close()
        assert task.done() and s.qzone.task is None

    asyncio.run(run())


def test_autonomous_unknown_price_warns_but_records_usage(s):
    assert s.budget.reserve(
        s.config.current, s.config.current.llm, "primary", "qzone_autonomous", 100, 50
    )
    assert s.budget.summary(s.config.current)["unknown_cost_calls"] == 1
    s.config.current.llm.input_price_cny_per_million = 2
    s.config.current.llm.output_price_cny_per_million = 8
    key = s.budget.reserve(
        s.config.current, s.config.current.llm, "primary", "qzone_autonomous", 100, 50
    )
    with s.db.connect() as db:
        assert (
            db.execute("SELECT purpose FROM llm_usage WHERE id=?", (key,)).fetchone()[0]
            == "qzone_autonomous"
        )
