import asyncio
import hashlib
import io
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.services.config_service import MemeSettings
from soulbot.services.media_service import MediaError
from soulbot.services.meme_extensions import MemeExtensions, sources, unpack
from soulbot.services.meme_policy import MemeRateLimiter


def tiny_archive(source):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("repo/LICENSE", "MIT")
        archive.writestr(f"repo/{source['directory']}/__init__.py", "")
    return buffer.getvalue()


def test_extension_source_catalog_is_fixed_and_opt_in():
    catalog = sources()
    assert set(catalog) == {"contrib", "emoji", "tudou"}
    assert all(len(item["commit"]) == 40 and len(item["sha256"]) == 64 for item in catalog.values())
    settings = MemeSettings()
    assert not any(item.enabled for item in settings.extensions.values())
    assert settings.max_generations_per_minute == 10
    assert settings.extensions["emoji"].max_generations_per_minute == 6
    with pytest.raises(ValidationError):
        MemeSettings(disabled_ids=["../outside"])
    with pytest.raises(ValidationError):
        MemeSettings(max_generations_per_minute=0)


@pytest.mark.parametrize(
    "name", ["../escape.py", "/absolute.py", "repo/../escape.py", "C:/outside.py"]
)
def test_extension_unpack_rejects_unsafe_paths(tmp_path, name):
    source = {"directory": "memes"}
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(name, "bad")
    with pytest.raises(MediaError):
        unpack(archive, tmp_path / "out", source)


def test_extension_unpack_requires_license_and_template_directory(tmp_path):
    archive = tmp_path / "missing.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("repo/other/file.py", "")
    with pytest.raises(MediaError, match="许可证"):
        unpack(archive, tmp_path / "out", {"directory": "memes"})


def test_extension_install_verifies_hash_and_publishes_atomically(tmp_path, monkeypatch):
    from soulbot.services import meme_extensions

    payload = tiny_archive({"directory": "memes"})
    digest = hashlib.sha256(payload).hexdigest()
    catalog = {
        "contrib": {
            "repository": "example/repo",
            "commit": "a" * 40,
            "sha256": digest,
            "directory": "memes",
            "archive_bytes": len(payload),
            "description": "test",
            "license": "MIT",
        }
    }
    monkeypatch.setattr(meme_extensions, "sources", lambda: catalog)
    service = build_services(tmp_path)
    manager = MemeExtensions(tmp_path / "third_party" / "meme-generator", service.config)

    async def handler(request):
        assert request.url.host == "codeload.github.com"
        return httpx.Response(200, content=payload)

    manager.transport = httpx.MockTransport(handler)
    result = asyncio.run(manager.install("contrib", lambda _: None))
    assert result["sha256"] == digest
    assert (
        tmp_path / "third_party/meme-generator/extensions/contrib" / result["release"] / "memes"
    ).is_dir()
    assert manager.installed("contrib")["commit"] == "a" * 40

    previous = manager.installed("contrib")
    cache = tmp_path / "third_party/meme-generator/extensions/cache" / (digest + ".zip")
    cache.write_bytes(b"corrupted cache")
    manager.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"tampered")
    )
    with pytest.raises(MediaError, match="校验不匹配"):
        asyncio.run(manager.install("contrib", lambda _: None))
    assert manager.installed("contrib") == previous


def test_meme_rate_limit_is_persistent_rolling_and_global(tmp_path):
    service = build_services(tmp_path)
    settings = service.config.current
    settings.meme.max_generations_per_minute = 2
    settings.meme.extensions["emoji"].max_generations_per_minute = 1
    service.config.save(settings)
    now = [1000.0]
    limiter = MemeRateLimiter(service.config, service.db, lambda: now[0])
    limiter.reserve("emoji", "preview")
    with pytest.raises(MediaError, match="扩展仓库"):
        limiter.reserve("emoji")
    MemeRateLimiter(service.config, service.db, lambda: now[0]).reserve("contrib")
    with pytest.raises(MediaError, match="全局"):
        MemeRateLimiter(service.config, service.db, lambda: now[0]).reserve("tudou")
    now[0] += 61
    MemeRateLimiter(service.config, service.db, lambda: now[0]).reserve("emoji")


def test_corrupt_meme_quota_fails_closed(tmp_path):
    service = build_services(tmp_path)
    service.db.set("meme_rate_window", "not-json")
    with pytest.raises(MediaError, match="限额记录无法读取"):
        MemeRateLimiter(service.config, service.db).reserve("contrib")


def test_extension_catalog_api_is_authenticated_and_install_is_audited(tmp_path, monkeypatch):
    services = build_services(tmp_path)

    async def fake_install(name):
        return {"repository": name, "status": "started"}

    monkeypatch.setattr(services.meme, "install_extension", fake_install)
    with TestClient(
        create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        assert client.get("/api/meme/extensions").status_code == 401
        assert (
            client.post(
                "/api/auth/setup",
                json={"username": "extension_admin", "password": "isolated-test-password-123"},
            ).status_code
            == 200
        )
        response = client.get("/api/meme/extensions")
        assert response.status_code == 200
        assert {item["id"] for item in response.json()["items"]} == {"contrib", "emoji", "tudou"}
        assert client.post("/api/meme/extensions/arbitrary/install").status_code == 422
        result = client.post("/api/meme/extensions/contrib/install")
        assert result.status_code == 200
        assert result.json()["status"] == "started"
        with services.db.connect() as connection:
            assert tuple(
                connection.execute(
                    "SELECT action, detail FROM audit ORDER BY id DESC LIMIT 1"
                ).fetchone()
            ) == ("meme_extension_requested", "contrib")
