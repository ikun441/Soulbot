import asyncio
import stat
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import BaseModel, ValidationError

from soulbot.app import create_web_app
from soulbot.core.permissions import PermissionEngine
from soulbot.providers.registry import ProviderRegistry
from soulbot.services.config_service import ConfigService, Settings
from soulbot.services.secret_service import SecretService
from soulbot.tools.registry import Tool, ToolContext, ToolRegistry

HEADERS = {"X-Soulbot-Request": "1"}
ACCOUNT = {"username": "tester", "password": "temporary-test-password-123"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_web_app(tmp_path), headers=HEADERS)


@pytest.fixture
def admin(client):
    assert client.post("/api/auth/setup", json=ACCOUNT).status_code == 200
    return client


def test_static_and_bootstrap(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    assert not client.get("/api/bootstrap").json()["has_admin"]
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/audit").status_code == 401


def test_account_is_one_time_and_password_not_plaintext(admin, tmp_path):
    assert admin.post("/api/auth/setup", json=ACCOUNT).status_code == 409
    with admin.app.state.services.db.connect() as db:
        row = db.execute("SELECT * FROM admins").fetchone()
        assert row["password_hash"] != ACCOUNT["password"]
        assert ACCOUNT["password"] not in (tmp_path / "data/soulbot.db").read_bytes().decode(
            errors="ignore"
        )
    cookie = admin.cookies.get("soulbot_session")
    with admin.app.state.services.db.connect() as db:
        assert db.execute("SELECT token_hash FROM sessions").fetchone()[0] != cookie
    assert "httponly" in admin.post("/api/auth/login", json=ACCOUNT).headers["set-cookie"].lower()


def test_logout_revokes_session(admin):
    token = admin.cookies.get("soulbot_session")
    assert admin.post("/api/auth/logout").status_code == 200
    admin.cookies.set("soulbot_session", token)
    assert admin.get("/api/config").status_code == 401


def test_validation_does_not_echo_password(client):
    value = "short-secret"
    r = client.post("/api/auth/setup", json={"username": "x", "password": value})
    assert r.status_code == 422
    assert value not in r.text


def test_cross_origin_and_missing_write_header(admin):
    assert (
        admin.post("/api/wizard/complete", headers={"Origin": "https://bad.example"}).status_code
        == 403
    )
    independent = TestClient(admin.app)
    assert independent.post("/api/auth/setup", json=ACCOUNT).status_code == 403
    assert admin.get("/api/bootstrap", headers={"Host": "bad.example"}).status_code == 400


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post("/api/auth/login", json=ACCOUNT).status_code == 401
    assert client.post("/api/auth/login", json=ACCOUNT).status_code == 429


def test_config_persists_and_wizard_resumes(admin, tmp_path):
    data = admin.get("/api/config").json()["config"]
    data["soulbot"]["name"] = "桃桃"
    assert not admin.put("/api/config", json=data).json()["restart_required"]
    assert admin.put("/api/wizard/progress", json={"step": 4}).status_code == 200
    token = admin.cookies.get("soulbot_session")
    restarted = TestClient(create_web_app(tmp_path), headers=HEADERS)
    restarted.cookies.set("soulbot_session", token)
    assert restarted.get("/api/config").json()["config"]["soulbot"]["name"] == "桃桃"
    assert restarted.get("/api/bootstrap").json()["wizard_step"] == 4
    assert restarted.post("/api/wizard/complete").status_code == 200
    assert restarted.get("/api/bootstrap").json()["initialized"]


def test_config_schema_and_restart_flag(admin):
    schema = admin.get("/api/config/schema").json()
    assert schema["additionalProperties"] is False
    data = admin.get("/api/config").json()["config"]
    data["web"]["port"] = 2626
    assert admin.put("/api/config", json=data).json()["restart_required"]
    assert admin.get("/api/health").json()["restart_required"]


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("web", "port", 80),
        ("soulbot", "timezone", "Not/AZone"),
        ("appearance", "wallpaper", "/etc/passwd"),
        ("qq", "test_url", "ws://user:secret@localhost"),
        ("llm", "base_url", "https://host?api_key=secret"),
        ("behavior", "autonomous", True),
        ("behavior", "daily_cost_cny", -1),
        ("personality", "owner_qq", "hello"),
    ],
)
def test_reject_invalid_config_without_overwriting(admin, tmp_path, section, key, value):
    old = (tmp_path / "config.yaml").read_bytes()
    data = admin.get("/api/config").json()["config"]
    data[section][key] = value
    assert admin.put("/api/config", json=data).status_code == 422
    assert (tmp_path / "config.yaml").read_bytes() == old


def test_backup_restore_and_export(admin):
    data = admin.get("/api/config").json()["config"]
    data["soulbot"]["name"] = "After"
    data["personality"]["owner_qq"] = "123456789"
    assert admin.put("/api/config", json=data).status_code == 200
    exported = admin.get("/api/config/export").text
    assert "123456789" not in exported
    assert admin.post("/api/config/restore").json()["config"]["soulbot"]["name"] == "SoulBot"
    assert admin.post("/api/config/restore").json()["config"]["soulbot"]["name"] == "After"


def test_secret_no_echo_and_permissions(admin, tmp_path):
    key = "private-test-credential"
    assert admin.put("/api/secrets", json={"llm_api_key": key}).status_code == 200
    assert admin.get("/api/config").json()["secrets"]["llm_api_key"]
    for path in ["/api/config", "/api/config/export", "/api/audit", "/api/bootstrap"]:
        assert key not in admin.get(path).text
    assert key not in (tmp_path / "config.yaml").read_text()
    if __import__("os").name != "nt":
        assert stat.S_IMODE((tmp_path / "data/secrets.json").stat().st_mode) == 0o600


def test_environment_secret_precedence(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SOULBOT_LLM_API_KEY=from-dotenv")
    secrets = SecretService(tmp_path)
    secrets.save({"llm_api_key": "from-json"})
    assert secrets.get("llm_api_key") == "from-dotenv"
    monkeypatch.setenv("SOULBOT_LLM_API_KEY", "from-env")
    assert secrets.get("llm_api_key") == "from-env"


def test_qq_allows_loopback_without_token_but_protects_remote(admin):
    data = admin.get("/api/config").json()["config"]
    data["qq"]["enabled"] = True
    assert admin.put("/api/config", json=data).status_code == 200
    data["web"]["host"] = "0.0.0.0"
    assert admin.put("/api/config", json=data).status_code == 422
    assert (
        admin.put("/api/secrets", json={"onebot_access_token": "local-test-token"}).status_code
        == 200
    )
    assert admin.put("/api/config", json=data).status_code == 200
    assert admin.put("/api/secrets", json={"onebot_access_token": ""}).status_code == 422


def test_wallpaper_upload_read_and_path_validation(admin):
    image = BytesIO()
    Image.new("RGB", (8, 8), "pink").save(image, format="PNG")
    result = admin.post(
        "/api/wallpaper", files={"file": ("中文壁纸.png", image.getvalue(), "image/png")}
    )
    assert result.status_code == 200
    url = result.json()["url"]
    assert admin.get(url).status_code == 200
    data = admin.get("/api/config").json()["config"]
    data["appearance"]["wallpaper"] = url
    assert admin.put("/api/config", json=data).status_code == 200
    assert (
        admin.post(
            "/api/wallpaper", files={"file": ("bad.svg", b"<svg/>", "image/svg+xml")}
        ).status_code
        == 422
    )
    assert (
        admin.post("/api/wallpaper", files={"file": ("bad.png", b"bad", "image/png")}).status_code
        == 422
    )
    assert admin.get("/api/wallpaper/not-a-file.png").status_code == 404


def test_unknown_and_unavailable_fields():
    with pytest.raises(ValidationError):
        Settings.model_validate({"providers": {}})
    with pytest.raises(ValidationError):
        Settings.model_validate({"behavior": {"qzone": True}})


def test_invalid_yaml_keeps_original(tmp_path):
    (tmp_path / "config.yaml").write_text("web: [broken")
    with pytest.raises(Exception):
        ConfigService(tmp_path)
    assert (tmp_path / "config.yaml").read_text() == "web: [broken"


class DummyProvider:
    capabilities = frozenset({"html_render"})

    def __init__(self):
        self.closed = False

    async def health_check(self):
        return True

    async def close(self):
        self.closed = True


def test_provider_capability_isolation_and_close():
    registry = ProviderRegistry()
    provider = DummyProvider()
    registry.register("html_render", "local", provider)
    assert registry.resolve("html_render") is provider
    with pytest.raises(ValueError):
        registry.register("image", "wrong", provider)
    with pytest.raises(LookupError):
        registry.resolve("image")
    with pytest.raises(ValueError):
        registry.register("html_render", "local", provider)
    asyncio.run(registry.close())
    assert provider.closed


class Arguments(BaseModel):
    text: str


def test_tool_permission_validation_limits_and_audit():
    events = []

    async def handler(args):
        return args.text

    registry = ToolRegistry(PermissionEngine(), lambda *args: events.append(args))
    registry.register(Tool("echo", "Echo", Arguments, handler, max_calls=1))
    context = ToolContext()
    with pytest.raises(PermissionError):
        asyncio.run(registry.execute("echo", {"text": "hello"}, context))
    registry.permissions.policies["echo"] = "approval"
    with pytest.raises(PermissionError):
        asyncio.run(registry.execute("echo", {"text": "hello", "approved": True}, context))
    registry.permissions.policies["echo"] = "auto"
    with pytest.raises(ValidationError):
        asyncio.run(registry.execute("echo", {}, context))
    assert asyncio.run(registry.execute("echo", {"text": "hello"}, context)) == "hello"
    with pytest.raises(PermissionError):
        asyncio.run(registry.execute("echo", {"text": "hello"}, context))
    assert [e[0] for e in events] == ["tool_started", "tool_completed"]


def test_tool_timeout_is_audited_and_consumes_budget():
    events = []

    async def handler(args):
        await asyncio.sleep(1)

    registry = ToolRegistry(PermissionEngine({"slow": "auto"}), lambda *args: events.append(args))
    registry.register(Tool("slow", "Slow", Arguments, handler, timeout=0.01))
    context = ToolContext(max_total=1)
    with pytest.raises(TimeoutError):
        asyncio.run(registry.execute("slow", {"text": "x"}, context))
    with pytest.raises(PermissionError):
        asyncio.run(registry.execute("slow", {"text": "x"}, context))
    assert events[-1][0] == "tool_failed"


def test_onebot_gate_disabled():
    from types import SimpleNamespace

    from soulbot.adapters.onebot_v11 import OneBotGate

    sent = []

    async def inner(scope, receive, send):
        raise AssertionError("Must not reach adapter")

    async def send(event):
        sent.append(event)

    gate = OneBotGate(inner, SimpleNamespace(running_config=Settings()))
    asyncio.run(gate({"type": "websocket", "path": "/onebot/v11/ws"}, None, send))
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_qq_probe_reports_unavailable(admin):
    # No service at a dynamically selected closed loopback port.
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = admin.get("/api/config").json()["config"]
    config["qq"]["connection"] = "forward"
    config["qq"]["test_url"] = f"ws://127.0.0.1:{port}/onebot/v11/ws"
    admin.put("/api/config", json=config)
    r = admin.post("/api/qq/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_version_consistency():
    import tomllib

    from soulbot import VERSION, __version__

    root = Path(__file__).resolve().parents[1]
    assert VERSION == "v" + __version__
    assert f"## {VERSION} - " in (root / "docs/version.me").read_text()
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    assert metadata["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "soulbot.__version__"


def test_token_change_restart_flag_survives_get(admin):
    admin.put("/api/secrets", json={"onebot_access_token": "new-token"})
    assert admin.get("/api/config").json()["restart_required"]
    assert admin.get("/api/health").json()["restart_required"]


def test_unicode_login_name_is_rejected_cleanly(admin):
    result = admin.post(
        "/api/auth/login", json={"username": "不存在的用户", "password": ACCOUNT["password"]}
    )
    assert result.status_code == 401


def test_reverse_connection_check_does_not_probe_its_own_receiver(admin, monkeypatch):
    import websockets

    def unexpected(*args, **kwargs):
        raise AssertionError("Reverse receiver must not be probed as a NapCat server")

    monkeypatch.setattr(websockets, "connect", unexpected)
    result = admin.post("/api/qq/test")
    assert result.status_code == 200 and not result.json()["ok"]
    assert result.json()["receiver_url"].endswith("/onebot/v11/ws")
    assert "反向连接" in result.json()["message"]


def test_tokenless_gate_rejects_remote_peer():
    import hashlib
    from types import SimpleNamespace

    from soulbot.adapters.onebot_v11 import OneBotGate

    settings = Settings()
    settings.qq.enabled = True
    services = SimpleNamespace(
        running_config=settings, running_token_digest=hashlib.sha256(b"").hexdigest()
    )
    sent = []

    async def inner(scope, receive, send):
        raise AssertionError("Remote peer cannot use tokenless adapter")

    async def send(event):
        sent.append(event)

    asyncio.run(
        OneBotGate(inner, services)(
            {"type": "websocket", "path": "/onebot/v11/ws", "client": ("192.0.2.9", 12345)},
            None,
            send,
        )
    )
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_remote_forward_connection_still_requires_token(admin):
    data = admin.get("/api/config").json()["config"]
    data["qq"].update(enabled=True, connection="forward", test_url="wss://napcat.example.org/ws")
    assert admin.put("/api/config", json=data).status_code == 422
