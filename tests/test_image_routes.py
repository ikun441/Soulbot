"""Image routing uses explicit HTTP fixtures; this does not prove a cloud account works."""

import asyncio
import base64
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_comfy_split import mock_comfy, png

from soulbot.app import build_services, create_web_app
from soulbot.providers.image.base import ImageGenerationError
from soulbot.services.backup_service import BackupService, InstanceLock
from soulbot.services.config_service import ImageGenerationEndpoint, ImageGenerationSettings
from soulbot.services.qzone_service import DraftCreate
from soulbot.storage.database import Database
from soulbot.tools.media_tools import specifications


@pytest.fixture
def s(tmp_path):
    services = build_services(tmp_path)
    cfg = services.config.current.image_generation
    cfg.enabled = True
    cfg.model = "primary-fixture"
    cfg.price_cny_per_image = 0.4
    cfg.max_calls_per_minute = 10
    cfg.fallbacks["fallback_1"] = ImageGenerationEndpoint(
        enabled=True,
        provider="comfyui",
        comfy={"checkpoint": "fixture.safetensors"},
        price_cny_per_image=0,
    )
    services.secrets.save(
        {
            "image_api_key": "primary-image-fixture",
            "image_fallback_1": "backup-image-fixture",
            "image_fallback_2": "second-image-fixture",
            "llm_api_key": "not-for-images",
        }
    )
    services.providers.resolve("image_generation", "comfyui").transport, state = mock_comfy()
    services.calls = []

    def primary(request):
        services.calls.append("primary")
        assert request.headers["authorization"] == "Bearer primary-image-fixture"
        return httpx.Response(401, text="private upstream body")

    services.providers.resolve(
        "image_generation", "openai_compatible"
    ).transport = httpx.MockTransport(primary)
    services.image_state = state
    return services


def run(s, **kwargs):
    return asyncio.run(s.images.generate("test prompt", **kwargs))


def rows(s):
    with s.db.connect() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT u.*,a.route_id,a.request_id FROM image_usage u LEFT JOIN media_attempts a ON a.usage_id=u.id ORDER BY u.rowid"
            )
        ]


def response():
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})


def test_cloud_rejection_uses_comfy_fixed_workflow_and_distinct_accounting(s):
    result = run(s)
    assert result.provider_id == "fallback_1" and result.provider == "comfyui"
    assert (
        result.data == png()
        and s.image_state["workflow"]["4"]["inputs"]["ckpt_name"] == "fixture.safetensors"
    )
    data = rows(s)
    assert [r["status"] for r in data] == ["rejected", "completed"]
    assert [r["route_id"] for r in data] == ["primary", "fallback_1"]
    assert len({r["request_id"] for r in data}) == 1 and all(r["cost_cny"] == 0 for r in data)
    assert s.images.router.status()[0]["state"] == "failed"
    assert s.images.router.status()[1]["state"] == "available"
    assert s.images.diagnostics()["state"] == "available"


@pytest.mark.parametrize("allow", [False, True])
def test_uncertain_generation_stops_by_default_and_can_be_explicitly_enabled(s, allow):
    s.config.current.image_generation.fallback_on_uncertain = allow
    s.providers.resolve("image_generation", "openai_compatible").transport = httpx.MockTransport(
        lambda _: httpx.Response(503)
    )
    if allow:
        assert run(s).provider_id == "fallback_1"
        assert len(rows(s)) == 2
    else:
        with pytest.raises(ImageGenerationError):
            run(s)
        assert len(rows(s)) == 1 and not s.image_state
    assert rows(s)[0]["status"] == "uncertain" and rows(s)[0]["cost_cny"] == 0.4


def test_successful_primary_never_generates_second_picture(s):
    s.providers.resolve("image_generation", "openai_compatible").transport = httpx.MockTransport(
        lambda _: response()
    )
    assert run(s).provider_id == "primary"
    assert len(rows(s)) == 1 and not s.image_state


def test_unconfigured_primary_is_skipped_before_reserving(s):
    s.config.current.image_generation.model = ""
    assert run(s).provider_id == "fallback_1"
    assert len(rows(s)) == 1 and rows(s)[0]["provider"] == "comfyui" and not s.calls


def test_single_target_never_switches_and_disabled_targets_do_not_reserve(s):
    with pytest.raises(ImageGenerationError):
        run(s, only="primary")
    assert len(rows(s)) == 1 and not s.image_state
    for target in ["other", "fallback_2"]:
        with pytest.raises(ImageGenerationError):
            run(s, only=target)
    assert len(rows(s)) == 1
    assert run(s, only="fallback_1").provider_id == "fallback_1"


def test_cloud_backups_use_independent_keys_models_and_prices(s):
    cfg = s.config.current.image_generation
    cfg.fallback_on_uncertain = True
    cfg.fallbacks["fallback_1"] = ImageGenerationEndpoint(
        enabled=True, base_url="https://one.example/v1", model="one", price_cny_per_image=None
    )
    cfg.fallbacks["fallback_2"] = ImageGenerationEndpoint(
        enabled=True, base_url="https://two.example/v1", model="two", price_cny_per_image=0.6
    )
    s.providers.resolve("image_generation", "openai_compatible").transport = httpx.MockTransport(
        lambda _: httpx.Response(503)
    )

    def backup(request):
        assert request.headers["authorization"] == "Bearer backup-image-fixture"
        assert request.url.host == "one.example"
        assert json.loads(request.content)["model"] == "one"
        return httpx.Response(503)

    def second(request):
        assert request.headers["authorization"] == "Bearer second-image-fixture"
        assert request.url.host == "two.example"
        assert json.loads(request.content)["model"] == "two"
        return response()

    s.providers.resolve("image_generation", "fallback_1").transport = httpx.MockTransport(backup)
    s.providers.resolve("image_generation", "fallback_2").transport = httpx.MockTransport(second)
    assert run(s).provider_id == "fallback_2"
    assert [r["cost_cny"] for r in rows(s)] == [0.4, None, 0.6]
    assert len({r["request_id"] for r in rows(s)}) == 1


def test_missing_backup_credentials_are_not_replaced_by_chat_or_primary_key(s):
    cfg = s.config.current.image_generation
    cfg.fallbacks["fallback_1"] = ImageGenerationEndpoint(
        enabled=True, base_url="https://one.example/v1", model="one"
    )
    s.secrets.save({"image_fallback_1": ""})
    with pytest.raises(ImageGenerationError):
        run(s)
    assert len(rows(s)) == 1 and rows(s)[0]["status"] == "rejected"
    assert s.images.router.status()[1]["state"] == "unconfigured"


@pytest.mark.parametrize("change", ["config", "secret", "pause", "deny", "approval"])
def test_live_policy_changes_prevent_backup_dispatch(s, change):
    def primary(request):
        if change == "config":
            s.config.current.image_generation.fallbacks["fallback_1"].timeout_seconds = 100
        elif change == "secret":
            s.secrets.save({"image_api_key": "changed-private-key"})
        elif change == "pause":
            s.config.current.automation.paused = True
        else:
            s.config.current.tools.generate_image = change
        return httpx.Response(401)

    s.providers.resolve("image_generation", "openai_compatible").transport = httpx.MockTransport(
        primary
    )
    with pytest.raises(ImageGenerationError):
        run(s, purpose="qzone_autonomous")
    assert len(rows(s)) == 1 and not s.image_state


def test_cancellation_never_switches_and_retains_uncertain_usage(s):
    async def scenario():
        entered = asyncio.Event()

        async def primary(request):
            entered.set()
            await asyncio.Event().wait()

        s.providers.resolve(
            "image_generation", "openai_compatible"
        ).transport = httpx.MockTransport(primary)
        task = asyncio.create_task(s.images.generate("test prompt"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(rows(s)) == 1 and rows(s)[0]["error_kind"] == "cancelled"
    assert rows(s)[0]["cost_cny"] == 0.4 and not s.image_state and not s.images.jobs


def test_cancel_comfy_deletes_only_own_queued_id_without_global_interrupt(s):
    cfg = s.config.current.image_generation
    cfg.provider = "comfyui"
    cfg.comfy.checkpoint = "fixture.safetensors"
    cfg.fallbacks = {}
    cfg.fallback_on_uncertain = True

    async def scenario():
        entered = asyncio.Event()
        calls = []
        transport, state = mock_comfy()

        async def dispatch(request):
            calls.append((request.url.path, request.content))
            if request.url.path.startswith("/history/"):
                entered.set()
                await asyncio.Event().wait()
            if request.url.path == "/queue":
                return httpx.Response(200, json={})
            return await transport.handle_async_request(request)

        s.providers.resolve("image_generation", "comfyui").transport = httpx.MockTransport(dispatch)
        task = asyncio.create_task(s.images.generate("test prompt"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        paths = [path for path, _ in calls]
        assert "/queue" in paths and "/interrupt" not in paths
        deletes = [json.loads(body)["delete"] for path, body in calls if path == "/queue"]
        assert len(deletes) == 1 and len(deletes[0]) == 1

    asyncio.run(scenario())
    assert rows(s)[0]["status"] == "uncertain" and not s.images.jobs


def test_total_timeout_stops_before_reserving_another_image(s):
    cfg = s.config.current.image_generation
    cfg.total_timeout_seconds = 0.02
    cfg.fallback_on_uncertain = True

    async def wait(request):
        await asyncio.sleep(1)
        return response()

    s.providers.resolve("image_generation", "openai_compatible").transport = httpx.MockTransport(
        wait
    )
    with pytest.raises(ImageGenerationError) as error:
        run(s)
    assert error.value.kind == "timeout" and len(rows(s)) == 1 and not s.image_state


def test_frequency_quota_counts_failed_and_backup_attempts(s):
    s.config.current.image_generation.max_calls_per_minute = 1
    with pytest.raises(ImageGenerationError) as error:
        run(s)
    assert error.value.kind == "quota" and len(rows(s)) == 1 and not s.image_state


@pytest.mark.parametrize("kind", ["size", "url_only", "parameters"])
def test_image_bounds_and_url_only_results_do_not_trigger_extra_generations(s, kind):
    s.config.current.image_generation.fallback_on_uncertain = True

    async def primary(*args):
        raise ImageGenerationError(kind, "Explicit local fixture", True)

    s.providers.resolve("image_generation", "openai_compatible").generate = primary
    with pytest.raises(ImageGenerationError):
        run(s)
    assert len(rows(s)) == 1 and not s.image_state


@pytest.mark.parametrize(
    "value",
    [
        {"provider": "html_render"},
        {"fallbacks": {"fallback_1": {"provider": "playwright"}}},
        {"fallbacks": {"other": {}}},
        {"fallbacks": {"fallback_1": {"enabled": True}}},
        {"fallbacks": {"fallback_1": {"comfy": {"base_url": "http://192.168.1.5:8188"}}}},
    ],
)
def test_schema_rejects_wrong_capability_duplicate_and_remote_comfy(value):
    with pytest.raises(ValidationError):
        ImageGenerationSettings.model_validate(value)


def test_disabled_backups_and_legacy_configuration_are_preserved(s):
    cfg = s.config.current.image_generation
    cfg.fallbacks["fallback_1"].enabled = False
    with pytest.raises(ImageGenerationError):
        run(s)
    assert len(rows(s)) == 1 and not s.image_state
    assert ImageGenerationSettings.model_validate({"provider": "comfyui"}).fallbacks == {}


def test_disabled_feature_cannot_be_overridden_by_working_backup(s):
    s.config.current.image_generation.enabled = False
    with pytest.raises(ImageGenerationError):
        run(s)
    assert not rows(s) and not s.image_state


def test_status_tracks_endpoint_secret_changes_and_no_prompts_or_keys(s):
    run(s)
    status = s.images.router.status()
    assert status[1]["state"] == "available"
    s.config.current.image_generation.fallbacks["fallback_1"].comfy.steps = 14
    assert s.images.router.status()[1]["state"] == "not_tested"
    assert s.images.diagnostics()["state"] == "not_tested"
    text = json.dumps(status)
    assert "primary-image-fixture" not in text and "test prompt" not in text


def test_model_description_considers_english_local_backup(s):
    assert s.images.needs_english()
    spec = next(item for item in specifications(s) if item["function"]["name"] == "generate_image")
    assert (
        "ComfyUI 候选" in spec["function"]["description"]
        and "英文" in spec["function"]["description"]
    )
    s.config.current.image_generation.fallbacks["fallback_1"].enabled = False
    assert not s.images.needs_english()


def test_api_protects_routes_supports_single_target_and_reports_actual_provider(s):
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as c:
        assert c.get("/api/image/status").status_code == 401
        c.post(
            "/api/auth/setup",
            json={"username": "tester", "password": "image-routes-fixture-password"},
        ).raise_for_status()
        result = c.post(
            "/api/image/generate", json={"prompt": "test prompt", "provider_id": "fallback_1"}
        )
        assert result.status_code == 200 and result.json()["provider_id"] == "fallback_1"
        assert result.json()["provider"] == "comfyui" and not s.calls
        assert c.get(result.json()["url"]).headers["content-type"] == "image/png"
        assert c.get("/api/image/status").json()["routes"][1]["state"] == "available"
        assert (
            c.post(
                "/api/image/generate", json={"prompt": "test prompt", "provider_id": "html_render"}
            ).status_code
            == 422
        )
        assert "primary-image-fixture" not in c.get("/api/config").text


def test_restore_keeps_newer_image_attempts_and_charges(s):
    cfg = s.config.current.image_generation
    cfg.fallback_on_uncertain = True
    s.config.save(s.config.current)
    backup = BackupService(s.root)
    snapshot = backup.snapshot()
    s.providers.resolve("image_generation", "openai_compatible").transport = httpx.MockTransport(
        lambda _: httpx.Response(503)
    )
    run(s)
    backup.stage(snapshot)
    with InstanceLock(s.root) as lock:
        backup.apply_pending(lock)
    again = build_services(s.root)
    data = rows(again)
    assert len(data) == 2 and data[0]["request_id"] == data[1]["request_id"]
    assert data[0]["cost_cny"] == 0.4
    assert (
        again.budget.summary(again.config.current)["image_generation"]["recent"][0]["route_id"]
        == "fallback_1"
    )


def test_migration_from_twenty_three_preserves_voice_attempt_details(s):
    with s.db.connect() as db:
        db.execute("DROP TABLE media_attempts")
        db.execute(
            "CREATE TABLE media_attempts(usage_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,capability TEXT NOT NULL CHECK(capability IN ('tts','asr')),route_id TEXT NOT NULL)"
        )
        db.execute("INSERT INTO media_attempts VALUES ('legacy','request','tts','primary')")
        db.execute("UPDATE metadata SET value='23' WHERE key='schema_version'")
    again = Database(s.db.path)
    Database(s.db.path)
    assert again.get("schema_version") == "24"
    with again.connect() as db:
        assert (
            db.execute("SELECT capability FROM media_attempts WHERE usage_id='legacy'").fetchone()[
                0
            ]
            == "tts"
        )
        db.execute(
            "INSERT INTO media_attempts VALUES ('new','request','image_generation','fallback_1')"
        )


def test_qzone_generation_lease_covers_complete_image_route(s):
    cfg = s.config.current
    cfg.qzone.auto_image = "generated"
    cfg.personality.owner_qq = "23456"
    cfg.image_generation.fallbacks["fallback_1"].timeout_seconds = 250
    cfg.image_generation.total_timeout_seconds = 300
    s.qzone.clock = lambda: 1000000

    async def complete(*args, **kwargs):
        with s.db.connect() as db:
            row = db.execute(
                "SELECT created_at,lease_until FROM qzone_drafts WHERE status='generating'"
            ).fetchone()
            assert row["lease_until"] - row["created_at"] == 150 + s.images.max_runtime()
        return SimpleNamespace(text="公开主题表达", tool_calls=[])

    async def images(*args):
        return []

    s.llm.complete = complete
    s.qzone.auto_images = images
    asyncio.run(
        s.qzone.create(
            DraftCreate(request_id=uuid4(), bot_id="34567", content="阳光", mode="ai"),
            autonomous=True,
        )
    )


def test_connection_failure_before_dispatch_can_switch_without_uncertain_opt_in(s):
    def connect(request):
        raise httpx.ConnectError("explicit connection fixture")

    s.providers.resolve("image_generation", "openai_compatible").transport = httpx.MockTransport(
        connect
    )
    assert run(s).provider_id == "fallback_1"
    assert rows(s)[0]["status"] == "rejected" and rows(s)[0]["cost_cny"] == 0


def test_failed_comfy_metadata_does_not_claim_a_submitted_task_or_block_backup(s):
    cfg = s.config.current.image_generation
    cfg.provider = "comfyui"
    cfg.comfy.checkpoint = "unreachable-fixture.safetensors"
    cfg.fallbacks["fallback_1"] = ImageGenerationEndpoint(
        enabled=True, base_url="https://backup.example/v1", model="backup-fixture"
    )
    calls = []

    def metadata(request):
        calls.append(request.url.path)
        return httpx.Response(503)

    s.providers.resolve("image_generation", "comfyui").transport = httpx.MockTransport(metadata)
    s.providers.resolve("image_generation", "fallback_1").transport = httpx.MockTransport(
        lambda _: response()
    )
    assert run(s).provider_id == "fallback_1"
    assert calls == ["/system_stats"] and rows(s)[0]["status"] == "rejected"
