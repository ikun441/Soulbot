import asyncio
import json
import wave
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.core.scheduler import JobCreate, JobReview, ProactiveScheduler
from soulbot.providers.llm.base import Completion
from soulbot.services.audio_service import AudioPayload, inspect_audio
from soulbot.services.backup_service import BackupService
from soulbot.services.media_service import ImagePayload, MediaError


def png(color="pink"):
    stream = BytesIO()
    Image.new("RGB", (24, 24), color).save(stream, format="PNG")
    return stream.getvalue()


def wav():
    stream = BytesIO()
    with wave.open(stream, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\x00\x00" * 800)
    return stream.getvalue()


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    s.now = [datetime(2026, 11, 5, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    s.scheduler.clock = s.consent.clock = s.media.clock = s.audio.clock = lambda: s.now[0]
    s.scheduler.rng = lambda a, b: 0
    s.config.current.qq.enabled = s.config.current.proactive.enabled = True
    s.config.current.behavior.global_send_interval_seconds = 0.1
    s.config.current.tts.enabled = True
    s.config.current.tools.synthesize_speech = "auto"
    s.qq_transport = SimpleNamespace(connected=lambda: ["34567"])
    s.sent = []

    async def sender(evt, payload):
        s.sent.append((evt, payload))

    s.agent.executor.sender = sender
    s.e = ChatEvent("34567", "23456", "consent", "", directed=True)
    s.consent.set_user(s.e, consent=True)
    return s


def image(s, scope="admin", source="upload"):
    return "media:" + s.media.save(png(), source, scope)["id"]


def audio(s):
    return "audio:" + s.audio.save(inspect_audio(wav()))["id"]


def create(s, refs=(), content="", earliest=None, latest=None, request_id=None):
    return s.scheduler.create(
        JobCreate(
            request_id=request_id or uuid4(),
            bot_id="34567",
            user_id="23456",
            content=content,
            attachments=[{"reference": ref} for ref in refs],
            earliest_at=earliest if earliest is not None else s.now[0],
            latest_at=latest if latest is not None else s.now[0] + 3600,
        )
    )["item"]


def approve(s, row):
    return s.scheduler.review(row["id"], JobReview(revision=row["revision"], action="approve"))


def current(s, key):
    return next(r for r in s.scheduler.listing()["items"] if r["id"] == key)


def test_media_only_fixed_snapshot_survives_source_expiry_and_restart(s):
    reference = image(s)
    row = create(s, [reference], earliest=s.now[0] + 86400, latest=s.now[0] + 3 * 86400)
    assert row["attachments"][0]["kind"] == "image" and "filename" not in json.dumps(row)
    approve(s, row)
    s.now[0] += 86401
    s.media.cleanup()
    with pytest.raises(MediaError):
        s.media.read(reference.split(":")[1])
    restarted = ProactiveScheduler(s, clock=s.scheduler.clock, rng=lambda a, b: 0)
    s.agent.executor.proactive_guard = restarted.allowed_event
    asyncio.run(restarted.tick())
    assert current(s, row["id"])["status"] == "sent"
    assert s.sent[0][1].data == png() and current(s, row["id"])["delivered_parts"] == 1
    asyncio.run(restarted.tick())
    assert len(s.sent) == 1


@pytest.mark.parametrize(
    "reference", ["media:../../secret", "https://example.invalid/image", "audio:1234"]
)
def test_reference_schema_rejects_paths_and_urls(s, reference):
    with pytest.raises(ValidationError):
        create(s, [reference])
    assert not list(s.scheduler.media.root.iterdir())


def test_other_conversation_media_cannot_be_pinned_and_failure_cleans_files(s):
    private = image(s, scope="34567:p:99999")
    with pytest.raises(MediaError):
        create(s, [image(s), private])
    assert s.scheduler.listing()["total"] == 0 and not list(s.scheduler.media.root.iterdir())


def test_idempotent_retry_uses_snapshot_after_original_disappears(s):
    from soulbot.core.social_state import StateConflict

    ref = image(s)
    request = uuid4()
    row = create(s, [ref], request_id=request)
    for p in s.media.directory.glob("*"):
        p.unlink()
    again = create(s, [ref], request_id=request)
    assert row["id"] == again["id"]
    with pytest.raises(StateConflict):
        create(s, [audio(s)], request_id=request)
    assert len(list(s.scheduler.media.root.iterdir())) == 1


def test_tampered_attachment_is_cancelled_before_any_send_or_reservation(s):
    row = approve(s, create(s, [image(s)], content="你好"))
    path = next(s.scheduler.media.directory(row["id"]).iterdir())
    path.write_bytes(b"tampered")
    asyncio.run(s.scheduler.tick())
    value = current(s, row["id"])
    assert value["status"] == "cancelled" and value["reason"] == "media_unavailable"
    assert not s.sent
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proactive_usage").fetchone()[0] == 0


def test_partial_delivery_is_persisted_and_never_retried(s):
    row = approve(s, create(s, [image(s)], content="你好"))

    async def sender(evt, payload):
        s.sent.append(payload)
        if isinstance(payload, ImagePayload):
            raise RuntimeError("private platform body")

    s.agent.executor.sender = sender
    asyncio.run(s.scheduler.tick())
    value = current(s, row["id"])
    assert value["status"] == "uncertain" and value["delivered_parts"] == 1
    asyncio.run(s.scheduler.tick())
    assert len(s.sent) == 2
    assert "private platform body" not in json.dumps(value)
    with s.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM proactive_usage").fetchone()[0] == 1


def test_consent_revoked_between_text_and_attachment_stops_remaining_parts(s):
    row = approve(s, create(s, [image(s)], content="你好"))

    async def sender(evt, payload):
        s.sent.append(payload)
        s.consent.set_user(s.e, consent=False)

    s.agent.executor.sender = sender
    asyncio.run(s.scheduler.tick())
    assert s.sent == ["你好"]
    assert current(s, row["id"])["status"] == "uncertain"
    assert current(s, row["id"])["delivered_parts"] == 1


def test_speech_waits_for_permission_then_sends_ai_label_and_audio(s):
    row = approve(s, create(s, [audio(s)]))
    s.config.current.tools.synthesize_speech = "deny"
    asyncio.run(s.scheduler.tick())
    assert current(s, row["id"])["reason"] == "media_disabled" and not s.sent
    s.config.current.tools.synthesize_speech = "auto"
    s.now[0] += 31
    asyncio.run(s.scheduler.tick())
    assert s.sent[0][1] == "【AI 合成语音】"
    assert isinstance(s.sent[1][1], AudioPayload) and current(s, row["id"])["status"] == "sent"
    assert current(s, row["id"])["delivered_parts"] == 1


def test_cancel_inflight_image_keeps_completed_text_and_usage(s):
    row = approve(s, create(s, [image(s)], content="你好"))

    async def run():
        entered = asyncio.Event()

        async def sender(evt, payload):
            s.sent.append(payload)
            if isinstance(payload, ImagePayload):
                entered.set()
                await asyncio.Future()

        s.agent.executor.sender = sender
        task = asyncio.create_task(s.scheduler.tick())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert current(s, row["id"])["status"] == "uncertain"
    assert current(s, row["id"])["delivered_parts"] == 1
    s.now[0] += 181
    asyncio.run(s.scheduler.tick())
    assert len(s.sent) == 2


def test_attachment_preview_requires_auth_and_backup_contains_pinned_bytes(s, tmp_path):
    row = create(s, [image(s), audio(s)])
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as c:
        assert c.get(row["attachments"][0]["url"]).status_code == 401
        c.post(
            "/api/auth/setup",
            json={"username": "media_admin", "password": "proactive-media-fixture"},
        )
        for item, expected in zip(row["attachments"], [png(), wav()], strict=True):
            result = c.get(item["url"])
            assert result.status_code == 200 and result.content == expected
            assert result.headers["cache-control"] == "no-store"
        assert c.get(f"/api/proactive/jobs/{row['id']}/attachments/2").status_code >= 400
    backup = BackupService(s.root)
    blob = backup.snapshot()
    files = backup.validate(blob)
    assets = [name for name in files if name.startswith("data/proactive_media/")]
    assert len(assets) == 2
    restored = tmp_path / "restored"
    restored.mkdir()
    for name, content in files.items():
        BackupService.write_bytes(restored / name, content)
    again = build_services(restored)
    assert (
        again.scheduler.media.read(row["id"], 0, again.scheduler.media.metadata(row["id"])).data
        == png()
    )
    next(s.scheduler.media.directory(row["id"]).iterdir()).unlink()
    with pytest.raises(ValueError, match="附件缺失"):
        backup.snapshot()


def test_unapproved_media_never_dispatches_and_rejected_assets_cleanup_is_bounded(s):
    row = create(s, [image(s)])
    asyncio.run(s.scheduler.tick())
    assert not s.sent
    s.scheduler.review(row["id"], JobReview(revision=row["revision"], action="reject"))
    s.scheduler.media.cleanup(s.now[0] + 86400)
    assert s.scheduler.media.directory(row["id"]).exists()
    s.scheduler.media.cleanup(row["latest_at"] + 7 * 86400 + 1)
    assert not s.scheduler.media.directory(row["id"]).exists()


def test_ai_selects_only_provided_emoji_and_keeps_global_relationship_context(s):
    s.config.current.proactive.auto_drafts = True
    s.config.current.proactive.auto_media = "emoji"
    item = s.emoji.upload("问候.png", png(), "开心")
    s.relationships.create("34567", "23456", "", "全局称呼", 66, True)
    seen = []

    async def complete(messages, **kw):
        seen.append((messages, kw))
        return Completion(json.dumps({"content": "想聊聊天吗？", "emoji_id": item["id"]}), None)

    s.llm.complete = complete
    asyncio.run(s.scheduler.tick())
    job = s.scheduler.listing()["items"][0]
    assert job["status"] == "pending_approval" and len(job["attachments"]) == 1
    assert "全局称呼" in seen[0][0][0]["content"] and seen[0][1]["action_event"].user_id == "23456"
    assert not s.sent


def test_ai_voice_revocation_during_synthesis_never_queues(s):
    s.config.current.proactive.auto_drafts = True
    s.config.current.proactive.auto_media = "voice"

    async def complete(*args, **kw):
        return Completion("想聊聊天吗？", None)

    async def synthesize(*args, **kw):
        s.consent.set_user(s.e, consent=False)
        return inspect_audio(wav())

    s.llm.complete = complete
    s.speech.synthesize = synthesize
    asyncio.run(s.scheduler.tick())
    assert not s.scheduler.listing()["items"] and not s.sent


def test_ai_voice_snapshot_is_queued_for_review_without_qq_send(s):
    s.config.current.proactive.auto_drafts = True
    s.config.current.proactive.auto_media = "voice"

    async def complete(*args, **kwargs):
        return Completion("今天想聊聊天吗？", None)

    async def speech(*args, **kwargs):
        return inspect_audio(wav())

    s.llm.complete = complete
    s.speech.synthesize = speech
    asyncio.run(s.scheduler.tick())
    item = s.scheduler.listing()["items"][0]
    assert item["status"] == "pending_approval" and item["attachments"][0]["kind"] == "audio"
    assert not s.sent


def test_ai_cannot_invent_an_emoji_reference(s):
    s.config.current.proactive.auto_drafts = True
    s.config.current.proactive.auto_media = "emoji"
    s.emoji.upload("问候.png", png(), "开心")

    async def complete(*args, **kwargs):
        return Completion(json.dumps({"content": "你好", "emoji_id": "f" * 32}), None)

    s.llm.complete = complete
    asyncio.run(s.scheduler.tick())
    assert not s.scheduler.listing()["items"] and not s.sent


def test_v19_upgrade_preserves_text_jobs_and_global_relationships(s):
    from soulbot.storage.database import Database

    row = create(s, content="旧文字草稿")
    relation = s.relationships.create("34567", "23456", "", "全局名称", 61, True)
    with s.db.connect() as db:
        for field in ["attachments_json", "delivered_parts"]:
            db.execute(f"ALTER TABLE proactive_jobs DROP COLUMN {field}")
        for field in ["remark", "is_friend", "friends_synced_at"]:
            db.execute(f"ALTER TABLE identity_accounts DROP COLUMN {field}")
        db.execute("DROP TABLE identity_aliases")
        db.execute("UPDATE metadata SET value='19' WHERE key='schema_version'")
    reopened = Database(s.db.path)
    assert reopened.get("schema_version") == "22"
    assert (
        current(s, row["id"])["attachments"] == [] and current(s, row["id"])["delivered_parts"] == 0
    )
    assert s.relationships.get(s.e)["affinity"] == relation["affinity"]
    Database(s.db.path)
