import asyncio
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
from soulbot.core.consent import ContactEdit, in_quiet_window
from soulbot.core.events import ChatEvent
from soulbot.core.scheduler import JobCreate, JobReview, ProactiveScheduler
from soulbot.core.social_state import StateConflict
from soulbot.storage.database import Database


@pytest.fixture
def services(tmp_path):
    s = build_services(tmp_path)
    s.now = [datetime(2026, 10, 27, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    s.scheduler.clock = s.consent.clock = lambda: s.now[0]
    s.scheduler.rng = lambda a, b: 0
    s.config.current.qq.enabled = True
    s.config.current.proactive.enabled = True
    s.config.current.behavior.global_send_interval_seconds = 0.1
    s.qq_transport = SimpleNamespace(connected=lambda: ["34567"])
    s.sent = []

    async def sender(event, payload):
        s.sent.append((event, payload))

    s.agent.executor.sender = sender
    s.consent.set_user(event(), consent=True)
    return s


def event(text="", user="23456", bot="34567"):
    return ChatEvent(bot, user, str(uuid4()), text, directed=True)


def draft(
    s,
    *,
    user="23456",
    bot="34567",
    content="今天过得怎么样？",
    earliest=None,
    latest=None,
    request_id=None,
):
    return JobCreate(
        request_id=request_id or uuid4(),
        bot_id=bot,
        user_id=user,
        content=content,
        earliest_at=s.now[0] if earliest is None else earliest,
        latest_at=s.now[0] + 3600 if latest is None else latest,
    )


def approved(s, **kwargs):
    row = s.scheduler.create(draft(s, **kwargs))["item"]
    return s.scheduler.review(row["id"], JobReview(revision=row["revision"], action="approve"))


def by_id(s, key):
    return next(row for row in s.scheduler.listing()["items"] if row["id"] == key)


def test_schema_nine_migration_preserves_existing_state_and_is_idempotent(services):
    s = services
    memory = s.memory.listing()["total"]
    s.db.set("preserved", "fixture")
    with s.db.connect() as db:
        for table in (
            "proactive_contacts",
            "proactive_jobs",
            "proactive_usage",
            "proactive_job_events",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("UPDATE metadata SET value='9' WHERE key='schema_version'")
    again = Database(s.db.path)
    assert again.get("schema_version") == "15" and again.get("preserved") == "fixture"
    assert s.memory.listing()["total"] == memory
    Database(again.path)


def test_missing_or_other_bot_consent_blocks_draft_creation(services):
    with pytest.raises(ValueError):
        services.scheduler.create(draft(services, user="99999"))
    with pytest.raises(ValueError):
        services.scheduler.create(draft(services, bot="99999"))
    with pytest.raises(ValueError):
        services.consent.set_user(replace(event(), group_id="12345"), consent=True)
    assert services.consent.get("99999", "23456") is None


def test_user_withdrawal_cancels_unexecuted_jobs_and_survives_restart(services):
    s = services
    one = approved(s)
    pending = s.scheduler.create(draft(s))["item"]
    s.consent.set_user(event(), consent=False)
    for key in (one["id"], pending["id"]):
        assert by_id(s, key)["status"] == "cancelled"
    restarted = ProactiveScheduler(s, clock=s.scheduler.clock)
    assert restarted.listing()["items"][0]["reason"] == "consent_revoked"
    s.consent.set_user(event(), consent=True)
    asyncio.run(s.scheduler.tick())
    assert not s.sent


def test_administrator_can_only_restrict_and_cannot_forge_opt_in(services):
    s = services
    contact = s.consent.get("34567", "23456")
    with pytest.raises(ValidationError):
        ContactEdit(revision=contact["revision"], opted_in=True)
    s.consent.edit(contact["id"], ContactEdit(revision=contact["revision"], admin_blocked=True))
    row = s.scheduler.create(draft(s))["item"]
    with pytest.raises(StateConflict):
        s.scheduler.review(row["id"], JobReview(revision=0, action="approve"))
    with pytest.raises(StateConflict):
        s.consent.edit(contact["id"], ContactEdit(revision=contact["revision"]))


def test_pending_approval_never_dispatches_and_review_has_revision_guard(services):
    s = services
    row = s.scheduler.create(draft(s))["item"]
    asyncio.run(s.scheduler.tick())
    assert not s.sent and by_id(s, row["id"])["status"] == "pending_approval"
    with pytest.raises(ValidationError):
        JobCreate(**{**draft(s).model_dump(), "approved_by": 1})
    row = s.scheduler.review(row["id"], JobReview(revision=0, action="approve"))
    with pytest.raises(StateConflict):
        s.scheduler.review(row["id"], JobReview(revision=0, action="cancel"))
    assert row["approved_by"] == 1


def test_create_retry_is_idempotent_and_rejects_changed_payload(services):
    s = services
    data = draft(s)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: s.scheduler.create(data), range(8)))
    assert sum(not item["duplicate"] for item in rows) == 1
    with pytest.raises(StateConflict):
        s.scheduler.create(data.model_copy(update={"content": "different"}))
    assert s.scheduler.listing()["total"] == 1


def test_send_is_once_and_usage_survives_repeated_ticks_and_restart(services):
    s = services
    row = approved(s)
    asyncio.run(s.scheduler.tick())
    asyncio.run(s.scheduler.tick())
    asyncio.run(ProactiveScheduler(s, clock=s.scheduler.clock).tick())
    assert len(s.sent) == 1 and by_id(s, row["id"])["status"] == "sent"
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proactive_usage").fetchone()[0] == 1
    with pytest.raises(StateConflict):
        s.scheduler.review(
            row["id"], JobReview(revision=by_id(s, row["id"])["revision"], action="cancel")
        )


@pytest.mark.parametrize(
    "setting,reason",
    [
        ("disabled", "global_disabled"),
        ("qq_off", "qq_disabled"),
        ("offline", "offline"),
        ("paused", "user_paused"),
        ("admin_blocked", "admin_blocked"),
        ("quiet", "quiet_hours"),
        ("unknown", "unknown_cost"),
        ("budget", "budget"),
    ],
)
def test_dynamic_restrictions_pause_before_any_reservation(services, setting, reason):
    s = services
    row = approved(s)
    if setting == "disabled":
        s.config.current.proactive.enabled = False
    if setting == "qq_off":
        s.config.current.qq.enabled = False
    if setting == "offline":
        s.qq_transport = None
    if setting == "paused":
        s.consent.set_user(event(), paused=True)
    if setting == "admin_blocked":
        c = s.consent.get("34567", "23456")
        s.consent.edit(c["id"], ContactEdit(revision=c["revision"], admin_blocked=True))
    if setting == "quiet":
        c = s.consent.get("34567", "23456")
        s.consent.edit(
            c["id"], ContactEdit(revision=c["revision"], quiet_start="12:00", quiet_end="14:00")
        )
    if setting in {"unknown", "budget"}:
        with s.db.connect() as db:
            db.execute(
                "INSERT INTO llm_usage(id,day,provider,model,purpose,status,input_tokens,output_tokens,cost_cny,created_at) VALUES ('fixture','2026-10-27','fixture','fixture','test','completed',1,1,?,?)",
                (None if setting == "unknown" else 5, s.now[0]),
            )
    asyncio.run(s.scheduler.tick())
    assert not s.sent and by_id(s, row["id"])["reason"] == reason
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proactive_usage").fetchone()[0] == 0


def test_time_window_jitter_expiry_and_resume_when_condition_changes(services):
    s = services
    s.scheduler.rng = lambda a, b: b
    row = approved(s, earliest=s.now[0] + 300, latest=s.now[0] + 900)
    assert row["run_at"] == s.now[0] + 420
    asyncio.run(s.scheduler.tick())
    assert not s.sent
    s.now[0] += 421
    s.consent.set_user(event(), paused=True)
    asyncio.run(s.scheduler.tick())
    assert by_id(s, row["id"])["status"] == "waiting"
    s.consent.set_user(event(), paused=False)
    s.now[0] += 31
    asyncio.run(s.scheduler.tick())
    assert len(s.sent) == 1
    other = approved(s, earliest=s.now[0] + 300, latest=s.now[0] + 400)
    s.now[0] += 401
    asyncio.run(s.scheduler.tick())
    assert by_id(s, other["id"])["status"] == "expired"


def test_quiet_hours_wrap_midnight_and_equal_times_mean_all_day():
    zone = "Asia/Shanghai"
    now = datetime(2026, 10, 27, 23, 0, tzinfo=ZoneInfo(zone)).timestamp()
    assert in_quiet_window(now, zone, "22:00", "07:00")
    assert in_quiet_window(now, zone, "00:00", "00:00")
    assert not in_quiet_window(now, zone, "01:00", "08:00")


def test_daily_per_user_and_global_caps_are_atomic_across_workers(services):
    s = services
    one = approved(s)
    two = approved(s)
    s.consent.set_user(event(user="45678"), consent=True)
    three = approved(s, user="45678")
    s.config.current.proactive.max_daily_total = 1
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(
            pool.map(lambda row: s.scheduler.claim(row["id"], ["34567"]), [one, two, three])
        )
    assert sum(row is not None for row in claims) == 1
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proactive_usage").fetchone()[0] == 1


def test_daily_and_cooldown_counters_persist_when_clock_crosses_midnight(services):
    s = services
    approved(s)
    asyncio.run(s.scheduler.tick())
    second = approved(s)
    asyncio.run(s.scheduler.tick())
    assert by_id(s, second["id"])["reason"] == "daily_user_limit"
    # A new statistics day does not reset the elapsed-time cooldown.
    s.now[0] += 13 * 3600
    s.config.current.behavior.quiet_hours = False
    c = s.consent.get("34567", "23456")
    s.consent.edit(
        c["id"], ContactEdit(revision=c["revision"], quiet_start="02:00", quiet_end="03:00")
    )
    third = approved(s)
    asyncio.run(s.scheduler.tick())
    assert by_id(s, third["id"])["reason"] == "cooldown"


def test_revocation_during_global_send_delay_blocks_dispatch(services, monkeypatch):
    s = services
    row = approved(s)
    monkeypatch.setattr(s.agent.decision, "reserve_send", lambda _: 0.01)
    real_sleep = asyncio.sleep

    async def revoke(delay):
        s.consent.set_user(event(), consent=False)
        await real_sleep(0)

    monkeypatch.setattr("soulbot.core.agent.asyncio.sleep", revoke)
    asyncio.run(s.scheduler.tick())
    assert not s.sent and by_id(s, row["id"])["status"] == "uncertain"


def test_sender_failure_is_uncertain_and_never_retried(services):
    s = services
    attempts = []

    async def failing(evt, text):
        attempts.append(evt)
        raise RuntimeError("private API body")

    s.agent.executor.sender = failing
    row = approved(s)
    asyncio.run(s.scheduler.tick())
    asyncio.run(s.scheduler.tick())
    assert len(attempts) == 1 and by_id(s, row["id"])["status"] == "uncertain"
    assert "private API body" not in str(s.scheduler.listing())


def test_crash_recovery_keeps_reserved_allowance_and_wont_replay(services):
    s = services
    row = approved(s)
    assert s.scheduler.claim(row["id"], ["34567"])
    s.now[0] += 91
    restarted = ProactiveScheduler(s, clock=s.scheduler.clock)
    asyncio.run(restarted.tick())
    assert not s.sent and by_id(s, row["id"])["reason"] == "interrupted"
    with s.db.connect() as db:
        assert db.execute("SELECT status FROM proactive_usage").fetchone()[0] == "uncertain"


def test_guard_rejects_forged_task_wrong_user_group_and_day_change(services):
    s = services
    row = approved(s)
    assert s.scheduler.claim(row["id"], ["34567"])
    evt = replace(event(), message_id="proactive:" + row["id"])
    assert s.scheduler.allowed_event(evt)
    assert not s.scheduler.allowed_event(replace(evt, user_id="45678"))
    assert not s.scheduler.allowed_event(replace(evt, group_id="12345"))
    assert not s.scheduler.allowed_event(replace(evt, message_id="proactive:forged"))
    # Timezone changes across the day boundary must not use yesterday's reservation.
    s.config.current.soulbot.timezone = "Pacific/Honolulu"
    s.config.current.behavior.quiet_hours = False
    assert not s.scheduler.allowed_event(evt)


def test_private_user_commands_revoke_even_during_reply_cooldown(services, monkeypatch):
    s = services
    row = approved(s)
    s.config.current.behavior.reply_cooldown_seconds = 600

    async def forbidden(*args, **kwargs):
        raise AssertionError("Consent commands must not call LLM")

    monkeypatch.setattr(s.llm, "complete", forbidden)

    async def check():
        for text in ["/主动联系 允许", "/免打扰 开启", "/主动联系 禁止"]:
            await s.agent.accept(event(text))

    asyncio.run(check())
    contact = s.consent.get("34567", "23456")
    assert not contact["opted_in"] and contact["user_paused"]
    assert by_id(s, row["id"])["status"] == "cancelled"
    before = s.consent.get("34567", "45678")
    asyncio.run(s.agent.accept(replace(event("/主动联系 允许", user="45678"), group_id="12345")))
    assert before is None and s.consent.get("34567", "45678") is None


def test_proactive_api_auth_review_and_no_consent_forgery(services):
    s = services
    c = TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"})
    assert c.get("/api/proactive/jobs").status_code == 401
    assert c.get("/api/proactive/contacts").status_code == 401
    assert (
        c.post(
            "/api/auth/setup",
            json={"username": "scheduler_test", "password": "scheduler-test-2026!"},
        ).status_code
        == 200
    )
    data = draft(s, content="私人草稿正文").model_dump(mode="json")
    assert c.post("/api/proactive/jobs", json={**data, "approved_by": 1}).status_code == 422
    row = c.post("/api/proactive/jobs", json=data).json()["item"]
    assert c.post("/api/proactive/jobs", json=data).json()["duplicate"]
    assert (
        c.post(
            f"/api/proactive/jobs/{row['id']}/review", json={"revision": 0, "action": "approve"}
        ).status_code
        == 200
    )
    assert (
        c.post(
            f"/api/proactive/jobs/{row['id']}/review", json={"revision": 0, "action": "cancel"}
        ).status_code
        == 409
    )
    contact = c.get("/api/proactive/contacts").json()["items"][0]
    assert (
        c.put(
            f"/api/proactive/contacts/{contact['id']}",
            json={"revision": contact["revision"], "opted_in": True},
        ).status_code
        == 422
    )
    assert "私人草稿正文" not in c.get("/api/audit").text
    assert "私人草稿正文" not in c.get("/api/config/export").text
    assert c.get("/api/proactive/jobs?status=bad").status_code == 422


def test_create_retry_after_expiry_returns_original_request(services):
    s = services
    data = draft(s)
    original = s.scheduler.create(data)
    s.now[0] += 4000
    retried = s.scheduler.create(data)
    assert retried["duplicate"] and retried["item"]["id"] == original["item"]["id"]
    assert retried["item"]["status"] == "expired"


def test_worker_startup_is_idempotent_and_shutdown_preserves_uncertainty(services):
    s = services
    row = approved(s)

    async def check():
        started = asyncio.Event()

        async def sender(evt, payload):
            started.set()
            await asyncio.Event().wait()

        s.agent.executor.sender = sender
        await s.scheduler.startup()
        first = s.scheduler.task
        await s.scheduler.startup()
        assert s.scheduler.task is first
        await asyncio.wait_for(started.wait(), 2)
        await s.scheduler.close()
        assert s.scheduler.task is None

    asyncio.run(check())
    assert by_id(s, row["id"])["reason"] == "interrupted"
    asyncio.run(ProactiveScheduler(s, clock=s.scheduler.clock).tick())
    assert by_id(s, row["id"])["status"] == "uncertain"


@pytest.mark.parametrize("disabled", ["private_chat", "qq"])
def test_withdrawal_is_saved_even_when_ordinary_chat_is_disabled(services, disabled):
    s = services
    row = approved(s)
    if disabled == "private_chat":
        s.config.current.behavior.private_chat = False
    else:
        s.config.current.qq.enabled = False
    asyncio.run(s.agent.accept(event("/主动联系 禁止")))
    assert not s.consent.get("34567", "23456")["opted_in"]
    assert by_id(s, row["id"])["status"] == "cancelled"
    assert not s.sent
