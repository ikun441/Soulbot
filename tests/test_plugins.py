"""Folder plugin execution and market integrity; network responses are contract fixtures."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.services.media_service import MediaError
from soulbot.services.plugin_manifest import Manifest
from soulbot.tools.media_tools import specifications
from soulbot.tools.registry import ToolContext


@pytest.fixture
def s(tmp_path):
    return build_services(tmp_path)


def package(s, plugin_id="echo"):
    root = s.plugins.root / plugin_id
    root.mkdir()
    marker = s.root / "imported.txt"
    manifest = {
        "api_version": 1,
        "define": {
            "name": "示例问候",
            "author": "fixture",
            "version": "v0.1.0",
            "info": "用于测试单文件夹加载",
        },
        "parameters": {
            "greeting": {"type": "string", "label": "前缀", "default": "你好", "max_length": 30},
            "repeat": {
                "type": "integer",
                "label": "次数",
                "default": 1,
                "minimum": 1,
                "maximum": 3,
            },
            "api_key": {"type": "string", "label": "密钥", "default": "", "secret": True},
        },
        "config": {"greeting": "你好"},
    }
    (root / "config.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True))
    (root / "plugin.py").write_text(f"""from pathlib import Path
from pydantic import BaseModel
Path({str(marker)!r}).write_text('loaded')
class Args(BaseModel):
    name: str

def register(api):
    async def greet(args,context):
        return {{'message': api.params['greeting']+args.name, 'repeat':api.params['repeat']}}
    api.tool(name='greet',description='问候',schema=Args,handler=greet,permission='auto')
""")
    (root / "README.md").write_text("Fixture package only")
    (root / "LICENSE").write_text("MIT")
    return root, manifest, marker


def test_scan_does_not_import_and_enable_requires_explicit_state(s):
    root, manifest, marker = package(s)
    item = s.plugins.listing()["items"][0]
    assert item["define"] == manifest["define"] and not item["enabled"]
    assert not marker.exists() and "plugin_echo__greet" not in s.tools.tools
    asyncio.run(s.plugins.enable("echo", 0))
    assert marker.exists()
    assert "plugin_echo__greet" in s.tools.tools
    assert s.tools.permissions.policy("plugin_echo__greet") == "approval"
    assert any(v["function"]["name"] == "plugin_echo__greet" for v in specifications(s))
    asyncio.run(s.plugins.disable("echo", 1))
    assert "plugin_echo__greet" not in s.tools.tools


def test_parameters_hot_update_secret_redaction_and_schema_validation(s):
    package(s)
    s.plugins.configure(
        "echo",
        0,
        {"greeting": "喵", "repeat": 2},
        {"api_key": "private-fixture-secret"},
        [],
        "auto",
    )
    asyncio.run(s.plugins.enable("echo", 1))
    value = asyncio.run(s.tools.execute("plugin_echo__greet", {"name": "朋友"}, ToolContext()))
    assert value == {"message": "喵朋友", "repeat": 2}
    item = s.plugins.listing()["items"][0]
    assert item["secrets"] == {"api_key": True} and "private-fixture-secret" not in json.dumps(item)
    assert s.plugins.parameters("echo")["api_key"] == "private-fixture-secret"
    with pytest.raises(ValueError):
        s.plugins.configure("echo", 2, {"repeat": True}, {}, [], "auto")
    with pytest.raises(ValueError):
        s.plugins.configure("echo", 2, {"repeat": 4}, {}, [], "auto")
    s.plugins.configure("echo", 2, {"greeting": "新前缀"}, {}, [], "auto")
    assert (
        asyncio.run(s.tools.execute("plugin_echo__greet", {"name": "朋友"}, ToolContext()))[
            "message"
        ]
        == "新前缀朋友"
    )
    with pytest.raises(ValueError):
        asyncio.run(
            s.tools.execute(
                "plugin_echo__greet", {"name": "朋友", "unexpected": True}, ToolContext()
            )
        )


def test_changed_code_is_not_executed_on_next_start(s):
    root, _, marker = package(s)
    asyncio.run(s.plugins.enable("echo", 0))
    asyncio.run(s.plugins.close())
    marker.unlink()
    with (root / "plugin.py").open("a") as f:
        f.write("\n# edited after approval\n")
    reopened = build_services(s.root)
    asyncio.run(reopened.plugins.startup())
    assert not marker.exists() and not reopened.plugins.state("echo")["enabled"]


def test_disable_revokes_stale_approval_and_global_switch(s):
    package(s)
    asyncio.run(s.plugins.enable("echo", 0))
    s.config.current.plugins.enabled = False
    assert s.tools.permissions.policy("plugin_echo__greet") == "deny"
    with pytest.raises(PermissionError):
        asyncio.run(s.tools.execute("plugin_echo__greet", {"name": "朋友"}, ToolContext()))
    assert not s.plugins.allowed("echo")


@pytest.mark.parametrize(
    "content",
    [
        b"api_version: 1\napi_version: 1\n",
        b"api_version: &x 1\ndefine: *x\n",
        b"!!python/object:os.system {}",
    ],
)
def test_manifest_rejects_duplicate_alias_and_custom_tags(content):
    with pytest.raises(Exception):
        Manifest.parse(content)


def test_manifest_rejects_inline_credentials(s):
    _, manifest, _ = package(s)
    manifest["config"]["api_key"] = "must-not-be-in-package"
    with pytest.raises(ValueError):
        Manifest.model_validate(manifest)


def test_installer_pins_commit_verifies_every_file_and_never_imports(s):
    folder, manifest, marker = package(s, "source")
    content = {p.name: p.read_bytes() for p in folder.iterdir()}
    meta = {
        "id": "echo",
        "path": "plugins/echo",
        **manifest["define"],
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in content.items()},
    }
    sha = "a" * 40
    urls = []

    def respond(request):
        urls.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"sha": sha})
        if request.url.path.endswith("/index.json"):
            return httpx.Response(200, json={"format": 1, "plugins": [meta]})
        return httpx.Response(200, content=content[Path(request.url.path).name])

    s.plugins.transport = httpx.MockTransport(respond)

    async def run():
        catalog = await s.plugins.refresh()
        assert catalog["commit"] == sha
        await s.plugins.install("echo", sha)

    asyncio.run(run())
    assert (s.plugins.root / "echo/config.yaml").is_file() and not marker.exists()
    assert not s.plugins.state("echo")["enabled"]
    assert all("/" + sha + "/" in url for url in urls if "raw.githubusercontent.com" in url)
    with pytest.raises(MediaError):
        s.plugins.begin_install("echo", sha)


def test_corrupted_package_does_not_install_or_execute(s):
    folder, manifest, marker = package(s, "source")
    content = {p.name: p.read_bytes() for p in folder.iterdir()}
    meta = {
        "id": "echo",
        "path": "plugins/echo",
        **manifest["define"],
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in content.items()},
    }
    sha = "b" * 40
    s.db.set(
        "plugin_catalog",
        json.dumps(
            {"repository": s.config.current.plugins.repository, "commit": sha, "items": [meta]}
        ),
    )
    s.plugins.transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"corrupt"))
    asyncio.run(s.plugins.install("echo", sha))
    assert not (s.plugins.root / "echo").exists() and not marker.exists()
    assert json.loads(s.db.get("plugin_install"))["status"] == "failed"


def test_api_requires_auth_acknowledgement_and_does_not_echo_secret(s):
    package(s)
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as c:
        assert c.get("/api/plugins").status_code == 401
        c.post(
            "/api/auth/setup",
            json={"username": "fixture", "password": "plugin-fixture-password-123"},
        )
        assert c.post("/api/plugins/echo/enable", json={"revision": 0}).status_code == 422
        assert (
            c.put(
                "/api/plugins/echo/config",
                json={
                    "revision": 0,
                    "values": {"repeat": 2},
                    "secrets": {"api_key": "private-api-secret"},
                    "policy": "auto",
                },
            ).status_code
            == 200
        )
        assert "private-api-secret" not in c.get("/api/plugins").text
        assert (
            c.post(
                "/api/plugins/echo/enable",
                json={"revision": 1, "acknowledge_python_execution": True},
            ).status_code
            == 200
        )
        assert c.post("/api/plugins/echo/disable", json={"revision": 2}).status_code == 200
