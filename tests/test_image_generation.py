import asyncio
import base64
import json
import sqlite3
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.providers.image.base import ImageGenerationError
from soulbot.services.config_service import ImageGenerationSettings
from soulbot.services.media_service import MediaError
from soulbot.tools.media_tools import specifications
from soulbot.tools.registry import ToolContext


def png():
    stream = BytesIO()
    Image.new("RGB", (64, 48), "pink").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def services(tmp_path):
    return build_services(tmp_path)


def enable(services):
    services.config.current.image_generation.enabled = True
    services.config.current.image_generation.model = "fixture-model"
    services.secrets.save({"image_api_key": "fixture-image-key"})
    services.providers.resolve("image_generation").transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]}
        )
    )


@pytest.mark.parametrize(
    "values",
    [
        {"size": "9999x9999"},
        {"size": "file:///a"},
        {"base_url": "file:///a"},
        {"timeout_seconds": float("inf")},
        {"price_cny_per_image": -1},
    ],
)
def test_image_settings_validate_url_size_price_and_time(values):
    with pytest.raises(ValidationError):
        ImageGenerationSettings.model_validate(values)


def test_disabled_unconfigured_and_invalid_prompt_do_not_reserve(services):
    with pytest.raises(ImageGenerationError, match="未启用"):
        asyncio.run(services.images.generate("test"))
    services.config.current.image_generation.enabled = True
    with pytest.raises(ImageGenerationError, match="独立 Key"):
        asyncio.run(services.images.generate("test"))
    enable(services)
    for prompt in [" ", "x" * 1001]:
        with pytest.raises(ImageGenerationError, match="提示词"):
            asyncio.run(services.images.generate(prompt))
    assert services.budget.summary(services.config.current)["image_generation"]["calls"] == 0


def test_provider_minimal_contract_independent_key_and_optional_capabilities(services):
    enable(services)
    services.secrets.save({"llm_api_key": "must-not-use", "tts_api_key": "must-not-use"})
    provider = services.providers.resolve("image_generation")
    bodies = []

    def respond(request):
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer fixture-image-key"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})

    provider.transport = httpx.MockTransport(respond)

    async def check():
        assert not await provider.health_check()
        result = await services.images.generate("private fixture prompt")
        assert (result.data, result.mime, result.width, result.height) == (
            png(),
            "image/png",
            64,
            48,
        )
        assert await provider.health_check()
        settings = services.config.current.image_generation
        settings.size, settings.quality = "1024x1024", "medium"
        settings.response_format, settings.output_format = "b64_json", "png"
        await services.images.generate("optional params")

    asyncio.run(check())
    assert bodies[0] == {"model": "fixture-model", "prompt": "private fixture prompt", "n": 1}
    assert bodies[1]["size"] == "1024x1024" and bodies[1]["quality"] == "medium"
    assert bodies[1]["response_format"] == "b64_json" and bodies[1]["output_format"] == "png"


def test_provider_diagnostics_track_real_attempts_and_invalidate_on_config_change(services):
    enable(services)
    image_service = services.images
    assert image_service.diagnostics()["state"] == "not_tested"
    services.config.current.image_generation.enabled = False
    assert image_service.diagnostics()["state"] == "disabled"
    services.config.current.image_generation.enabled = True
    assert image_service.diagnostics()["state"] == "not_tested"

    asyncio.run(image_service.generate("fixture success"))
    diagnostics = image_service.diagnostics()
    assert diagnostics["state"] == "available" and diagnostics["last_test_at"]
    assert "fixture-image-key" not in str(diagnostics)
    services.config.current.image_generation.model = "changed-model"
    assert image_service.diagnostics() == {
        "state": "not_tested",
        "last_test_at": None,
        "last_error_kind": None,
    }

    services.providers.resolve("image_generation").transport = httpx.MockTransport(
        lambda request: httpx.Response(401, text="secret provider body")
    )
    with pytest.raises(ImageGenerationError):
        asyncio.run(image_service.generate("fixture failure"))
    diagnostics = image_service.diagnostics()
    assert diagnostics["state"] == "failed" and diagnostics["last_error_kind"] == "authentication"
    services.secrets.save({"image_api_key": "rotated-key"})
    assert image_service.diagnostics()["state"] == "not_tested"


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limit"),
        (400, "parameters"),
        (404, "parameters"),
        (503, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_provider_errors_are_safe_no_retry_or_redirect(services, status, kind):
    enable(services)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="private provider error text",
            headers={"location": "http://127.0.0.1/private"},
        )

    services.providers.resolve("image_generation").transport = httpx.MockTransport(respond)
    with pytest.raises(ImageGenerationError) as error:
        asyncio.run(services.images.generate("private prompt"))
    assert error.value.kind == kind and "private" not in str(error.value) and len(calls) == 1
    row = services.budget.summary(services.config.current)["image_generation"]["recent"][0]
    assert row["status"] == ("uncertain" if status in {503, 302} else "rejected")


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        [1],
        [{"b64_json": "%%%"}],
        [{"b64_json": base64.b64encode(b"not an image").decode()}],
        [{"b64_json": "x"}, {"b64_json": "y"}],
    ],
)
def test_malformed_base64_or_non_image_response_is_uncertain(services, data):
    enable(services)
    services.providers.resolve("image_generation").transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": data})
    )
    with pytest.raises(ImageGenerationError) as error:
        asyncio.run(services.images.generate("test"))
    assert error.value.kind == "format" and error.value.uncertain


def test_url_only_response_never_causes_download_and_retains_cost(services):
    enable(services)
    services.config.current.image_generation.price_cny_per_image = 0.5
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"url": "http://127.0.0.1/private"}]})

    services.providers.resolve("image_generation").transport = httpx.MockTransport(respond)
    with pytest.raises(ImageGenerationError) as error:
        asyncio.run(services.images.generate("test"))
    assert error.value.kind == "url_only" and len(calls) == 1
    row = services.budget.summary(services.config.current)["image_generation"]["recent"][0]
    assert row["status"] == "uncertain" and row["cost_cny"] == 0.5


def test_response_stream_size_limit_stops_reception(services, monkeypatch):
    import soulbot.providers.image.openai_compatible as module

    enable(services)
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 256)
    services.providers.resolve("image_generation").transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 257)
    )
    with pytest.raises(ImageGenerationError) as error:
        asyncio.run(services.images.generate("test"))
    assert error.value.kind == "size" and error.value.uncertain


def test_tools_scoped_single_call_and_html_is_not_a_fallback(services):
    enable(services)
    assert "html_render" not in services.providers.resolve("image_generation").capabilities
    assert "image_generation" not in services.renderer.capabilities
    assert any(item["function"]["name"] == "generate_image" for item in specifications(services))

    async def check():
        context = ToolContext(scope="private:fixture")
        result = await services.tools.execute(
            "generate_image", {"prompt": "private prompt"}, context
        )
        key = result["attachment"].split(":")[1]
        assert services.media.read(key, context.scope).data == png()
        with pytest.raises(MediaError):
            services.media.read(key, "group:other")
        with pytest.raises(PermissionError):
            await services.tools.execute("generate_image", {"prompt": "again"}, context)
        services.config.current.tools.generate_image = "deny"
        with pytest.raises(PermissionError):
            await services.tools.execute("generate_image", {"prompt": "denied"}, ToolContext())

    asyncio.run(check())


def test_api_auth_preview_private_prompt_not_in_logs_or_usage(services, tmp_path):
    enable(services)
    with TestClient(
        create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        assert client.get("/api/image/status").status_code == 401
        assert (
            client.post("/api/image/generate", json={"prompt": "private prompt"}).status_code == 401
        )
        client.post(
            "/api/auth/setup",
            json={"username": "image_fixture", "password": "image-test-password-123"},
        )
        result = client.post("/api/image/generate", json={"prompt": "private fixture prompt"})
        assert result.status_code == 200
        item = result.json()
        assert item["source"] == "image_generation" and client.get(item["url"]).content == png()
        assert "private fixture prompt" not in client.get("/api/audit").text
        assert "private fixture prompt" not in client.get("/api/usage").text
        assert "api_key" not in client.get("/api/image/status").text


def test_shared_cost_and_unknown_price_warn_without_stopping_calls(services):
    enable(services)
    settings = services.config.current
    assert asyncio.run(services.images.generate("proactive", "proactive")).data == png()
    settings.image_generation.price_cny_per_image = 1
    settings.behavior.daily_cost_cny = 1
    key = services.images.reserve(settings, "fixture")
    services.images.finish(key, "completed")
    assert services.budget.reserve(settings, settings.llm, "primary", "explicit", 1, 1)
    assert services.speech.reserve(settings, "test", "explicit")
    assert services.recognition.reserve(settings, 0.1, "explicit")
    assert any(
        item["code"] == "daily_cost" for item in services.budget.summary(settings)["warnings"]
    )


def test_cancellation_concurrency_persistence_and_daily_limit(services, monkeypatch, tmp_path):
    enable(services)
    services.config.current.image_generation.max_calls_per_minute = 10
    services.config.current.image_generation.price_cny_per_image = 0.2

    async def check():
        entered = asyncio.Event()

        async def wait(prompt, settings):
            entered.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(services.providers.resolve("image_generation"), "generate", wait)
        one = asyncio.create_task(services.images.generate("one"))
        await entered.wait()
        two = asyncio.create_task(services.images.generate("two"))
        await asyncio.sleep(0)
        with pytest.raises(ImageGenerationError, match="任务较多"):
            await services.images.generate("three")
        await services.images.close()
        assert one.cancelled() and two.cancelled() and not services.images.jobs

    asyncio.run(check())
    again = build_services(tmp_path)
    again.config.current.image_generation.max_images_per_day = 2
    with pytest.raises(ImageGenerationError, match="额度"):
        again.images.reserve(again.config.current, "fixture")
    rows = again.budget.summary(again.config.current)["image_generation"]["recent"]
    assert all(row["status"] == "uncertain" and row["cost_cny"] == 0.2 for row in rows)


def test_generated_image_send_rechecks_off_switch_after_rate_delay(services, monkeypatch):
    enable(services)
    services.config.current.qq.enabled = True
    executor = services.agent.executor
    sent = []

    async def sender(event, payload):
        sent.append(payload)

    def reserve(settings):
        settings.image_generation.enabled = False
        return 0.001

    executor.sender = sender
    monkeypatch.setattr(executor.decision, "reserve_send", reserve)
    artifact = services.media.save(png(), "image_generation")
    event = ChatEvent("12345", "3497689533", "test", "")
    assert not asyncio.run(
        executor.send_image(event, services.media.read(artifact["id"]), "admin", "generate_image")
    )
    assert not sent


def test_generic_send_api_cannot_bypass_generated_image_off_switch(services, tmp_path):
    enable(services)
    artifact = services.media.save(png(), "image_generation")
    services.config.current.image_generation.enabled = False
    services.config.current.personality.owner_qq = "3497689533"

    class Transport:
        @staticmethod
        def connected():
            return ["12345"]

    services.qq_transport = Transport()
    with TestClient(
        create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        client.post(
            "/api/auth/setup",
            json={"username": "image_fixture", "password": "image-test-password-123"},
        )
        result = client.post(
            "/api/media/send-test", json={"confirm": True, "image": "media:" + artifact["id"]}
        )
        assert result.status_code == 403


def test_schema_six_migration_preserves_admin_session_and_existing_usage(services, tmp_path):
    services.auth.setup("image_admin", "migration-test-password-123")
    token = services.auth.session()
    previous = services.recognition.reserve(services.config.current, 1, "fixture")
    with services.db.connect() as db:
        admin = tuple(db.execute("SELECT * FROM admins").fetchone())
        db.execute("DROP TABLE image_usage")
        db.execute("UPDATE metadata SET value='6' WHERE key='schema_version'")
    again = build_services(tmp_path)
    assert again.auth.valid(token) and again.db.get("schema_version") == "17"
    with sqlite3.connect(again.db.path) as db:
        assert db.execute("SELECT * FROM admins").fetchone() == admin
        assert db.execute("SELECT id FROM asr_usage").fetchone()[0] == previous
    pending = again.images.reserve(again.config.current, "fixture")
    third = build_services(tmp_path)
    row = third.budget.summary(third.config.current)["image_generation"]["recent"][0]
    assert (
        row["id"] == pending and row["status"] == "uncertain" and row["error_kind"] == "interrupted"
    )


def test_unknown_image_cost_does_not_block_proactive_llm(services):
    enable(services)
    settings = services.config.current
    key = services.images.reserve(settings, "fixture")
    services.images.finish(key, "completed")
    settings.llm.input_price_cny_per_million = 0
    settings.llm.output_price_cny_per_million = 0
    assert services.budget.reserve(settings, settings.llm, "primary", "proactive", 1, 1)
    assert services.budget.summary(settings)["unknown_cost_calls"] == 1


def test_image_timeout_retains_cost_and_does_not_retry(services, monkeypatch):
    enable(services)
    services.config.current.image_generation.price_cny_per_image = 0.3
    calls = []

    async def timeout(prompt, settings):
        calls.append(True)
        raise TimeoutError

    monkeypatch.setattr(services.providers.resolve("image_generation"), "generate", timeout)
    with pytest.raises(ImageGenerationError, match="超时"):
        asyncio.run(services.images.generate("timeout"))
    row = services.budget.summary(services.config.current)["image_generation"]["recent"][0]
    assert row["status"] == "uncertain" and row["cost_cny"] == 0.3 and len(calls) == 1
