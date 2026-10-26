import asyncio
import json
import sys
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.services.media_service import MediaError
from soulbot.services.render_service import HTMLRenderService
from soulbot.tools.html_render import CardRequest, card_html
from soulbot.tools.media_tools import specifications
from soulbot.tools.registry import ToolContext


def png():
    stream = BytesIO()
    Image.new("RGB", (800, 100), "pink").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def services(tmp_path):
    return build_services(tmp_path)


@pytest.mark.parametrize(
    "values",
    [
        {"title": "x", "body": "text", "html": "<script>run()</script>"},
        {"title": "x", "body": "text", "theme": "url(file:///etc/passwd)"},
        {"title": "x", "body": ""},
        {"title": " ", "body": "text"},
        {"template": "list", "title": "x", "items": []},
        {"template": "list", "title": "x", "items": ["x"] * 13},
        {"template": "list", "title": "x", "items": ["x" * 121]},
    ],
)
def test_card_schema_rejects_arbitrary_markup_settings_and_invalid_content(values):
    with pytest.raises(ValidationError):
        CardRequest.model_validate(values)


def test_all_user_text_is_escaped_in_fixed_template():
    card = CardRequest(
        template="list",
        title="<script>window.pwned=1</script>",
        body='<img src="file:///private/secret">',
        items=['<iframe src="https://external.invalid">'],
        footer="</style><script>run()</script>",
    )
    html = card_html(card, "fixed-font-bytes")
    assert "<script>" not in html
    assert "<img " not in html
    assert "<iframe " not in html
    assert "&lt;script&gt;" in html and "&lt;iframe" in html
    assert "default-src 'none'" in html


def test_render_api_requires_auth_and_disabled_feature_does_not_launch(services, tmp_path):
    with TestClient(
        create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        assert client.get("/api/render/status").status_code == 401
        assert client.post("/api/render/action", json={"action": "install"}).status_code == 401
        assert (
            client.post("/api/render/generate", json={"title": "x", "body": "text"}).status_code
            == 401
        )
        assert (
            client.post(
                "/api/auth/setup",
                json={"username": "render_admin", "password": "render-test-password-123"},
            ).status_code
            == 200
        )
        assert not client.get("/api/render/status").json()["enabled"]
        result = client.post("/api/render/generate", json={"title": "x", "body": "text"})
        assert result.status_code == 422 and "未启用" in result.json()["detail"]
        assert not services.renderer.processes
        assert client.get("/api/health").status_code == 200


def test_render_provider_capability_and_tool_permission_scope(services, monkeypatch):
    services.config.current.html_render.enabled = True
    renderer = services.providers.resolve("html_render")
    assert renderer is services.renderer
    assert services.providers.resolve("image_generation") is not renderer
    assert "html_render" not in services.providers.resolve("image_generation").capabilities
    assert not services.config.current.image_generation.enabled
    calls = []

    async def fake_render(card):
        calls.append(card)
        return png()

    monkeypatch.setattr(renderer, "render", fake_render)
    assert not any(item["function"]["name"] == "render_html" for item in specifications(services))
    monkeypatch.setattr(renderer, "ready", lambda: True)
    assert any(item["function"]["name"] == "render_html" for item in specifications(services))
    schema = next(
        item["function"]["parameters"]
        for item in specifications(services)
        if item["function"]["name"] == "render_html"
    )
    assert "title" in schema["properties"]
    assert set(schema["required"]) <= set(schema["properties"])
    assert "title" not in schema

    async def check():
        context = ToolContext(scope="private:fixture-a")
        result = await services.tools.execute(
            "render_html", {"title": "私密卡片", "body": "只属于当前会话"}, context
        )
        key = result["attachment"].split(":", 1)[1]
        assert services.media.read(key, "private:fixture-a").mime == "image/png"
        with pytest.raises(MediaError):
            services.media.read(key, "group:fixture-b")
        services.config.current.tools.render_html = "deny"
        with pytest.raises(PermissionError):
            await services.tools.execute("render_html", {"title": "x", "body": "text"}, context)
        assert len(calls) == 1
        assert not any(
            item["function"]["name"] == "render_html" for item in specifications(services)
        )

    asyncio.run(check())


def test_render_concurrency_queue_and_cleanup_are_bounded(services, monkeypatch):
    renderer = services.renderer
    services.config.current.html_render.enabled = True
    monkeypatch.setattr(renderer, "ready", lambda: True)

    async def check():
        release = asyncio.Event()
        active, maximum = 0, 0

        async def command(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await release.wait()
                return 0, png(), b""
            finally:
                active -= 1

        monkeypatch.setattr(renderer, "command", command)
        tasks = [
            asyncio.create_task(renderer.render(CardRequest(title="x", body="text")))
            for _ in range(4)
        ]
        await asyncio.sleep(0)
        assert renderer.pending == 4
        with pytest.raises(MediaError, match="任务较多"):
            await renderer.render(CardRequest(title="x", body="text"))
        release.set()
        await asyncio.gather(*tasks)
        assert maximum == 2 and renderer.pending == 0 and not renderer.jobs
        await renderer.close()

    asyncio.run(check())


def test_render_timeout_terminates_owned_subprocess(services, monkeypatch):
    renderer = services.renderer
    services.config.current.html_render.enabled = True
    services.config.current.html_render.timeout_seconds = 0.1
    monkeypatch.setattr(renderer, "ready", lambda: True)
    original_command = renderer.command

    async def slow_command(*args, **kwargs):
        return await original_command([sys.executable, "-c", "import time; time.sleep(30)"])

    monkeypatch.setattr(renderer, "command", slow_command)

    async def check():
        with pytest.raises(MediaError, match="超时"):
            await renderer.render(CardRequest(title="x", body="text"))
        assert not renderer.processes and not renderer.jobs and renderer.pending == 0
        await renderer.close()

    asyncio.run(check())


def test_shutdown_cancels_render_and_install_releases_file_lock(services, tmp_path, monkeypatch):
    renderer = services.renderer

    async def check():
        async def wait_command(*args, **kwargs):
            await asyncio.sleep(30)

        monkeypatch.setattr(renderer, "command", wait_command)
        await renderer.begin_install()
        await asyncio.sleep(0)
        await renderer.cancel_install()
        other = HTMLRenderService(tmp_path, services.config, services.db.audit)
        assert other.lock.acquire()
        other.lock.close()
        services.config.current.html_render.enabled = True
        monkeypatch.setattr(renderer, "ready", lambda: True)
        task = asyncio.create_task(renderer.render(CardRequest(title="x", body="text")))
        await asyncio.sleep(0)
        await renderer.close()
        assert task.cancelled()
        assert not renderer.jobs and renderer.pending == 0

    asyncio.run(check())


def test_render_child_environment_excludes_bot_secrets(services, monkeypatch):
    monkeypatch.setenv("SOULBOT_LLM_API_KEY", "private-test-key")
    monkeypatch.setenv("SOULBOT_WEBUI_PASSWORD", "private-test-password")
    environment = services.renderer.environment()
    assert "SOULBOT_LLM_API_KEY" not in environment
    assert "SOULBOT_WEBUI_PASSWORD" not in environment
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == str(services.renderer.root / "browsers")


def test_corrupt_ready_manifest_and_font_symlink_are_rejected(services, tmp_path):
    renderer = services.renderer
    renderer.root.mkdir()
    (renderer.root / "ready.json").write_text(json.dumps({"version": "1.64.1", "executable": None}))
    assert not renderer.ready()
    outside = tmp_path / "outside-font"
    outside.write_text("must not change")
    (renderer.root / "NotoSansSC-Regular.ttf").symlink_to(outside)
    with pytest.raises(MediaError, match="符号链接"):
        asyncio.run(renderer.prepare_font())
    assert outside.read_text() == "must not change"
