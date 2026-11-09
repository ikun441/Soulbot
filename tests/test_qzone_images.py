"""Local fixtures exercise image publication; no test publishes to a QQ account."""

import asyncio
import base64
import json
import os
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import Completion
from soulbot.services.backup_service import BackupService, InstanceLock
from soulbot.services.media_service import ImagePayload, MediaError
from soulbot.services.qzone_images import QZoneImageInput, QZoneImages
from soulbot.services.qzone_service import DraftCreate, DraftEdit, DraftPublish, DraftReview
from soulbot.storage.database import Database
from soulbot.tools.registry import ToolContext


def png(color="pink", size=(24, 24)):
    stream = BytesIO()
    Image.new("RGBA", size, color).save(stream, format="PNG")
    return stream.getvalue()


class ImageTransport:
    def __init__(self):
        self.sent = []
        self.images_supported = True
        self.after_probe = None
        self.result = {"tid": "local_fixture"}

    def connected(self):
        return ["34567"]

    async def qzone_capability(self, bot_id):
        if self.after_probe:
            self.after_probe()
        return {"supported": True, "images_supported": self.images_supported, "connection": self}

    async def qzone_publish(self, bot_id, text, ugc, connection, guard, images=None):
        assert connection is self and guard()
        self.sent.append((bot_id, text, ugc, images or []))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    s.now = [1793937600.0]
    s.qzone.clock = s.media.clock = lambda: s.now[0]
    s.config.current.personality.owner_qq = "23456"
    s.config.current.qq.enabled = s.config.current.qzone.enabled = True
    s.qq_transport = ImageTransport()
    return s


def reference(s, color="pink", scope="admin", source="upload"):
    return "media:" + s.media.save(png(color), source, scope)["id"]


def create(s, refs=(), **kwargs):
    return asyncio.run(
        s.qzone.create(
            DraftCreate(
                request_id=kwargs.pop("request_id", uuid4()),
                bot_id="34567",
                content="阳光与猫咪",
                images=[{"reference": ref} for ref in refs],
                **kwargs,
            )
        )
    )


def approve(s, row):
    return s.qzone.review(
        row["id"],
        DraftReview(
            revision=row["revision"],
            action="approve",
            public_confirmed=True,
        ),
    )


def publish(s, row):
    return asyncio.run(s.qzone.publish(row["id"], DraftPublish(revision=row["revision"])))


def stored(s, key):
    with s.db.connect() as db:
        return dict(s.qzone.row(db, key))


def image_path(s, row):
    value = stored(s, row["id"])
    return s.qzone.images.directory(value["images_key"]) / "0.jpg"


def autonomous(s, mode):
    cfg = s.config.current
    cfg.qzone.require_approval = False
    cfg.qzone.autonomous_enabled = True
    cfg.qzone.auto_bot_id = "34567"
    cfg.qzone.auto_audience = "public"
    cfg.qzone.auto_topics = ["阳光与猫咪"]
    cfg.qzone.auto_image = mode
    cfg.behavior.quiet_hours = False

    async def complete(*args, **kwargs):
        return Completion("阳光照进窗户，适合读一本喜欢的书。", None)

    s.llm.complete = complete


def test_schema_twenty_upgrade_preserves_text_draft(s):
    row = create(s)
    with s.db.connect() as db:
        db.execute("ALTER TABLE qzone_drafts DROP COLUMN images_key")
        db.execute("ALTER TABLE qzone_drafts DROP COLUMN images_json")
        db.execute("UPDATE metadata SET value='20' WHERE key='schema_version'")
    again = Database(s.db.path)
    Database(s.db.path)
    assert again.get("schema_version") == "23"
    assert s.qzone.get(row["id"])["content"] == row["content"]
    assert s.qzone.get(row["id"])["images"] == []


def test_fixed_copy_survives_expiry_restart_and_idempotent_retry(s):
    ref, request = reference(s), uuid4()
    row = create(s, [ref], request_id=request)
    assert row["images"][0]["mime"] == "image/jpeg"
    assert "sha256" not in json.dumps(row) and "images_key" not in row
    s.now[0] += 86401
    s.media.cleanup()
    assert create(s, [ref], request_id=request)["id"] == row["id"]
    restarted = build_services(s.root)
    assert restarted.qzone.image_payloads(stored(s, row["id"]))[0].mime == "image/jpeg"
    with pytest.raises(StateConflict):
        create(s, [reference(s, "blue")], request_id=request)


def test_normalization_bounds_removes_metadata_and_flattens_transparency():
    stream = BytesIO()
    source = Image.new("RGB", (2600, 1200), "pink")
    exif = Image.Exif()
    exif[270] = "private camera description"
    source.save(stream, format="JPEG", exif=exif)
    data, width, height = QZoneImages.normalize(stream.getvalue())
    assert (width, height) == (2048, 945)
    with Image.open(BytesIO(data)) as result:
        assert not result.getexif() and result.format == "JPEG"
    data, _, _ = QZoneImages.normalize(png((0, 0, 0, 0)))
    with Image.open(BytesIO(data)) as result:
        assert min(result.getpixel((0, 0))) > 245


@pytest.mark.parametrize(
    "ref", ["media:../../a", "file:///a", "https://a/image", "audio:" + "a" * 32]
)
def test_reference_validation_rejects_urls_paths_and_audio(ref):
    with pytest.raises(ValidationError):
        QZoneImageInput(reference=ref)


def test_four_images_rejected_before_any_copy(s):
    with pytest.raises(ValidationError):
        create(s, [reference(s)] * 4)
    assert not list(s.qzone.images.root.iterdir())


def test_other_scope_failure_rolls_back_entire_draft_and_files(s):
    with pytest.raises(MediaError):
        create(s, [reference(s), reference(s, scope="34567:p:99999")])
    assert s.qzone.listing()["total"] == 0
    assert not list(s.qzone.images.root.iterdir())


@pytest.mark.parametrize("source", ["upload", "avatar", "meme"])
def test_model_cannot_publish_private_photos_or_avatar_as_attachment(s, source):
    ref = reference(s, scope="34567:p:23456", source=source)
    with pytest.raises(MediaError, match="私人素材"):
        asyncio.run(
            s.qzone.create(
                DraftCreate(
                    request_id=uuid4(),
                    bot_id="34567",
                    content="公开主题",
                    images=[{"reference": ref}],
                ),
                from_chat=True,
                image_scope="34567:p:23456",
            )
        )
    assert not s.qq_transport.sent and not list(s.qzone.images.root.iterdir())


def test_owner_tool_accepts_generated_media_and_only_saves_draft(s):
    s.config.current.tools.create_qzone_draft = "auto"
    event = ChatEvent("34567", "23456", "local-tool-fixture", "画一张公开插画", directed=True)
    ref = reference(s, scope=event.scope, source="image_generation")
    asyncio.run(
        s.tools.execute(
            "create_qzone_draft",
            {
                "content": "阳光与猫咪",
                "images": [{"reference": ref}],
            },
            ToolContext(scope=event.scope, event=event),
        )
    )
    row = s.qzone.listing()["items"][0]
    assert len(row["images"]) == 1 and row["status"] == "pending_approval"
    assert not s.qq_transport.sent


def test_replace_and_clear_invalidate_approval_and_old_previews(s):
    row = approve(s, create(s, [reference(s)]))
    path = image_path(s, row)
    kept = s.qzone.edit(
        row["id"],
        DraftEdit(
            revision=row["revision"],
            content=row["content"],
            audience="public",
        ),
    )
    assert image_path(s, kept) == path
    row = approve(s, kept)
    replaced = s.qzone.edit(
        row["id"],
        DraftEdit(
            revision=row["revision"],
            content=row["content"],
            audience="public",
            images=[{"reference": reference(s, "blue")}],
        ),
    )
    assert image_path(s, replaced) != path and replaced["approved_at"] is None
    with pytest.raises(StateConflict):
        publish(s, row)
    cleared = s.qzone.edit(
        row["id"],
        DraftEdit(
            revision=replaced["revision"],
            content=row["content"],
            audience="public",
            images=[],
        ),
    )
    assert cleared["images"] == [] and stored(s, row["id"])["images_key"] == ""


@pytest.mark.parametrize("failure", ["missing", "tampered", "symlink"])
def test_invalid_copy_never_approves_or_reserves_publication(s, failure):
    row = create(s, [reference(s)])
    path = image_path(s, row)
    if failure == "tampered":
        path.write_bytes(png("blue"))
    else:
        path.unlink()
        if failure == "symlink":
            path.symlink_to(s.db.path)
    with pytest.raises(MediaError):
        approve(s, row)
    s.config.current.qzone.require_approval = False
    with pytest.raises(MediaError):
        publish(s, row)
    assert not s.qq_transport.sent and s.qzone.status()["used_last_seven_days"] == 0


def test_image_capability_absence_blocks_before_quota(s):
    row = approve(s, create(s, [reference(s)]))
    s.qq_transport.images_supported = False
    with pytest.raises(ValueError, match="图片能力"):
        publish(s, row)
    assert not s.qq_transport.sent and s.qzone.status()["used_last_seven_days"] == 0


def test_image_changed_during_capability_check_is_blocked(s):
    row = approve(s, create(s, [reference(s)]))
    s.qq_transport.after_probe = lambda: image_path(s, row).write_bytes(b"changed")
    with pytest.raises(MediaError):
        publish(s, row)
    assert not s.qq_transport.sent and s.qzone.status()["used_last_seven_days"] == 0


def test_three_fixed_images_publish_with_public_audience_without_forging_review(s):
    s.config.current.qzone.require_approval = False
    row = create(s, [reference(s, color) for color in ["pink", "blue", "red"]], audience="public")
    result = publish(s, row)
    assert result["status"] == "published" and result["approved_at"] is None
    sent = s.qq_transport.sent[0]
    assert sent[2] == 1 and len(sent[3]) == 3
    assert all(isinstance(img, ImagePayload) and img.mime == "image/jpeg" for img in sent[3])
    with pytest.raises(StateConflict):
        publish(s, result)
    assert len(s.qq_transport.sent) == 1


@pytest.mark.parametrize(
    "result", [{}, TimeoutError("private upstream body"), asyncio.CancelledError()]
)
def test_uncertain_image_attempt_never_retries_or_refunds_quota(s, result):
    row = approve(s, create(s, [reference(s)]))
    s.qq_transport.result = result
    if isinstance(result, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            publish(s, row)
    else:
        assert publish(s, row)["status"] == "uncertain"
    assert s.qzone.get(row["id"])["status"] == "uncertain"
    assert s.qzone.status()["used_last_seven_days"] == 1
    with pytest.raises(StateConflict):
        publish(s, s.qzone.get(row["id"]))
    assert len(s.qq_transport.sent) == 1


def test_native_wire_uses_base64_images_and_rechecks_guard(monkeypatch):
    from soulbot.adapters.onebot_v11 import NoneBotTransport

    calls = []

    async def call_api(action, **params):
        calls.append((action, params))
        return {"tid": "fixture"}

    bot = SimpleNamespace(call_api=call_api)
    monkeypatch.setattr("nonebot.get_bot", lambda _: bot)
    data, _, _ = QZoneImages.normalize(png())
    payload = ImagePayload(data, "image/jpeg")
    transport = NoneBotTransport()
    asyncio.run(transport.qzone_publish("34567", "公开文字", 1, bot, lambda: True, [payload]))
    assert calls[0][0] == "send_qzone_msg"
    wire = calls[0][1]
    assert wire["ugc_right"] == 1 and wire["images"][0].startswith("base64://")
    assert base64.b64decode(wire["images"][0][9:]) == data
    checks = iter([True, False])
    with pytest.raises(ValueError):
        asyncio.run(
            transport.qzone_publish("34567", "公开文字", 1, bot, lambda: next(checks), [payload])
        )
    assert len(calls) == 1


def test_private_preview_requires_auth_revision_and_image_exists(s):
    row = create(s, [reference(s)])
    app = create_web_app(s.root, s)
    with TestClient(app, headers={"X-Soulbot-Request": "1"}) as c:
        url = row["images"][0]["url"]
        assert c.get(url).status_code == 401
        assert (
            c.post(
                "/api/auth/setup", json={"username": "tester", "password": "local-fixture-password"}
            ).status_code
            == 200
        )
        response = c.get(url)
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "no-store"
        assert c.get(url.replace("/images/0", "/images/4")).status_code == 404
        s.qzone.edit(
            row["id"], DraftEdit(revision=row["revision"], content="新公开正文", audience="public")
        )
        assert c.get(url).status_code == 409
        assert c.get("/api/qzone/drafts/9999/images/0?revision=0").status_code == 404


def test_backup_restore_keeps_private_copy_and_missing_copy_blocks_backup(s):
    row = create(s, [reference(s)])
    backup = BackupService(s.root)
    content = backup.snapshot()
    assert any(name.startswith("data/qzone_images/") for name in backup.validate(content))
    other = s.root / "restored"
    other.mkdir()
    build_services(other)
    restored = BackupService(other)
    restored.stage(content)
    with InstanceLock(other) as lock:
        restored.apply_pending(lock)
    again = build_services(other)
    assert again.qzone.image_payloads(stored(again, row["id"]))[0].mime == "image/jpeg"
    image_path(s, row).unlink()
    with pytest.raises(ValueError, match="图片缺失"):
        backup.snapshot()


def test_cleanup_retains_pending_and_unknown_but_removes_old_terminal_and_orphan(s):
    row = create(s, [reference(s)])
    pending = image_path(s, row).parent
    orphan = s.qzone.images.root / uuid4().hex
    orphan.mkdir()
    os.utime(orphan, (s.now[0] - 90000, s.now[0] - 90000))
    s.qzone.images.cleanup(s.now[0] + 31 * 86400)
    assert pending.exists() and not orphan.exists()
    cancelled = s.qzone.review(row["id"], DraftReview(revision=row["revision"], action="cancel"))
    s.qzone.images.cleanup(cancelled["updated_at"] + 31 * 86400)
    assert not pending.exists()


def test_auto_emoji_pins_and_publishes_once(s):
    autonomous(s, "emoji")
    s.config.current.tools.select_emoji = "auto"
    s.emoji.upload("阳光.png", png(), "开心")
    asyncio.run(s.qzone.tick())
    row = s.qzone.listing()["items"][0]
    assert row["status"] == "published" and len(row["images"]) == 1
    assert s.qq_transport.sent[0][2] == 1
    asyncio.run(s.qzone.tick())
    assert len(s.qq_transport.sent) == 1


@pytest.mark.parametrize("revoke", [False, True])
def test_auto_generated_images_use_only_public_topic_and_recheck_policy(s, revoke):
    autonomous(s, "generated")
    s.config.current.image_generation.enabled = True
    s.config.current.tools.generate_image = "auto"
    s.images.configured = lambda: True
    seen = []

    async def generate(prompt, purpose):
        seen.append((prompt, purpose))
        if revoke:
            s.config.current.qzone.auto_image = "none"
        return ImagePayload(png(), "image/png")

    s.images.generate = generate
    asyncio.run(s.qzone.tick())
    row = s.qzone.listing()["items"][0]
    assert seen == [
        (
            "绘制适合公开分享的插画，不含姓名、联系方式或私人身份信息。公开主题：阳光与猫咪",
            "qzone_autonomous",
        )
    ]
    assert row["status"] == ("pending_approval" if revoke else "published")
    assert len(row["images"]) == (0 if revoke else 1)
    assert len(s.qq_transport.sent) == (0 if revoke else 1)
