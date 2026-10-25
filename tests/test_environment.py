import asyncio
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets
from fastapi import HTTPException
from fastapi.testclient import TestClient
from websockets.exceptions import InvalidStatus

from soulbot.app import build_services, create_web_app
from soulbot.services.auth_service import AuthService, verify_password
from soulbot.services.config_service import ConfigService, Settings
from soulbot.services.environment_service import (
    ACCOUNT_KEYS,
    PASSWORD,
    PASSWORD_HASH,
    REVERSE_WS,
    USERNAME,
    EnvironmentError,
    EnvironmentService,
)
from soulbot.storage.database import Database

ACCOUNT = {"username": "env_admin", "password": "isolated-env-password-123"}
HEADERS = {"X-Soulbot-Request": "1"}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ACCOUNT_KEYS | {REVERSE_WS}:
        monkeypatch.delenv(name, raising=False)


def test_dotenv_account_import_hashes_literal_password_and_preserves_other_values(tmp_path):
    password = "literal-${DO_NOT_EXPAND}-password"
    path = tmp_path / ".env"
    path.write_text(
        "# keep this comment\nSOULBOT_LLM_API_KEY='existing-key'\nMULTILINE='first\nsecond'\n"
        f"{USERNAME}=env_admin\n{PASSWORD}='{password}'\n",
        encoding="utf-8",
    )
    services = build_services(tmp_path)
    token = services.auth.login("env_admin", password, "fixture")
    assert services.auth.valid(token)
    stored = EnvironmentService(tmp_path).values()
    assert PASSWORD not in stored
    assert verify_password(password, stored[PASSWORD_HASH])
    assert stored["MULTILINE"] == "first\nsecond"
    assert "# keep this comment" in path.read_text()
    assert password not in path.read_text()
    assert services.secrets.get("llm_api_key") == "existing-key"
    assert build_services(tmp_path).auth.valid(token)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_existing_database_account_migrates_without_resetting_password_or_sessions(tmp_path):
    database = Database(tmp_path / "data/soulbot.db")
    previous = AuthService(database)
    previous.setup(**ACCOUNT)
    token = previous.login(**ACCOUNT, client="fixture")
    current = build_services(tmp_path)
    assert current.auth.valid(token)
    assert current.auth.account()["stored_in_env"]
    assert current.auth.login(**ACCOUNT, client="fixture")
    assert verify_password(
        ACCOUNT["password"], EnvironmentService(tmp_path).values()[PASSWORD_HASH]
    )


def test_manual_env_password_reset_revokes_old_sessions_only_when_changed(tmp_path):
    services = build_services(tmp_path)
    services.auth.setup(**ACCOUNT)
    token = services.auth.login(**ACCOUNT, client="fixture")
    EnvironmentService(tmp_path).update({PASSWORD: "replacement-password-123"})
    restarted = build_services(tmp_path)
    assert not restarted.auth.valid(token)
    with pytest.raises(HTTPException):
        restarted.auth.login(**ACCOUNT, client="fixture")
    token = restarted.auth.login(ACCOUNT["username"], "replacement-password-123", "fixture")
    assert build_services(tmp_path).auth.valid(token)


def test_process_account_overrides_file_and_blocks_web_updates(tmp_path, monkeypatch):
    EnvironmentService(tmp_path).update({USERNAME: "file_admin", PASSWORD: "file-password-123"})
    monkeypatch.setenv(USERNAME, "process_admin")
    monkeypatch.setenv(PASSWORD, "process-password-123")
    services = build_services(tmp_path)
    assert services.auth.login("process_admin", "process-password-123", "fixture")
    assert services.auth.account()["managed_by_process"]
    with pytest.raises(HTTPException) as caught:
        services.auth.change_account("changed_admin", None, "process-password-123", "fixture")
    assert caught.value.status_code == 409
    assert "process-password-123" not in (tmp_path / ".env").read_text()


@pytest.mark.parametrize(
    "values",
    [
        {USERNAME: "only_username"},
        {PASSWORD: "secret-password-without-user"},
        {USERNAME: "bad user", PASSWORD: "valid-password-123"},
        {USERNAME: "valid_user", PASSWORD: "tiny-pass"},
        {USERNAME: "valid_user", PASSWORD_HASH: "999999999$invalid-secret"},
    ],
)
def test_invalid_account_env_fails_without_echoing_credentials(tmp_path, values):
    EnvironmentService(tmp_path).update(values)
    with pytest.raises(EnvironmentError) as caught:
        build_services(tmp_path)
    assert ".env" in str(caught.value) or "SOULBOT_WEBUI" in str(caught.value)
    for value in values.values():
        assert value not in str(caught.value)


def test_account_api_persists_hash_rotates_sessions_and_never_exports_password(tmp_path):
    services = build_services(tmp_path)
    with TestClient(create_web_app(tmp_path, services), headers=HEADERS) as client:
        assert client.get("/api/auth/account").status_code == 401
        assert client.put("/api/auth/account", json={}).status_code == 401
        assert client.post("/api/auth/setup", json=ACCOUNT).status_code == 200
        old_session = client.cookies.get("soulbot_session")
        other_session = services.auth.session()
        previous_env = (tmp_path / ".env").read_bytes()
        changes = {
            "username": "updated_admin",
            "password": "updated-password-123",
            "current_password": "wrong-password",
        }
        assert client.put("/api/auth/account", json=changes).status_code == 403
        assert (tmp_path / ".env").read_bytes() == previous_env
        assert services.auth.valid(old_session)
        changes["current_password"] = ACCOUNT["password"]
        response = client.put("/api/auth/account", json=changes)
        assert response.status_code == 200, response.text
        assert client.get("/api/auth/account").json()["username"] == "updated_admin"
        assert not services.auth.valid(old_session)
        assert not services.auth.valid(other_session)
        assert client.cookies.get("soulbot_session") != old_session
        for path in [
            "/api/bootstrap",
            "/api/auth/account",
            "/api/config",
            "/api/config/export",
            "/api/audit",
        ]:
            text = client.get(path).text
            assert "updated-password-123" not in text
            assert PASSWORD_HASH not in text
            assert "password_hash" not in text
        assert client.get("/.env").status_code == 404
        values = EnvironmentService(tmp_path).values()
        assert values[USERNAME] == "updated_admin"
        assert verify_password("updated-password-123", values[PASSWORD_HASH])
        assert PASSWORD not in values
    restarted = build_services(tmp_path)
    assert restarted.auth.login("updated_admin", "updated-password-123", "fixture")


def test_reverse_ws_save_restore_and_reload_are_shared_with_env(tmp_path):
    config = ConfigService(tmp_path)
    settings = config.current.model_copy(deep=True)
    settings.qq.reverse_ws_url = "wss://bot.example.org/soulbot/qq/ws"
    config.save(settings)
    assert EnvironmentService(tmp_path).get(REVERSE_WS) == settings.qq.reverse_ws_url
    assert ConfigService(tmp_path).current.qq.reverse_ws_url == settings.qq.reverse_ws_url
    EnvironmentService(tmp_path).update({REVERSE_WS: ""})
    assert ConfigService(tmp_path).current.qq.reverse_ws_url == ""
    config.restore()
    assert EnvironmentService(tmp_path).get(REVERSE_WS) == ""
    restarted = ConfigService(tmp_path)
    assert (
        restarted.current.qq.receiver_url(restarted.current.web)
        == "ws://127.0.0.1:2526/onebot/v11/ws"
    )


def test_reverse_ws_env_precedence_and_process_override_cannot_be_silently_saved(
    tmp_path, monkeypatch
):
    EnvironmentService(tmp_path).update({REVERSE_WS: "wss://file.example.org/qq/ws"})
    monkeypatch.setenv(REVERSE_WS, "wss://process.example.org/bot/ws")
    config = ConfigService(tmp_path)
    assert config.current.qq.reverse_ws_url == "wss://process.example.org/bot/ws"
    previous = (tmp_path / ".env").read_bytes()
    changes = config.current.model_copy(deep=True)
    changes.qq.reverse_ws_url = "ws://127.0.0.1:2526/custom/ws"
    with pytest.raises(EnvironmentError, match="进程环境变量"):
        config.save(changes)
    assert (tmp_path / ".env").read_bytes() == previous


@pytest.mark.parametrize(
    "url",
    [
        "https://bot.example.org/ws",
        "ws://user:private-password@bot.example.org/ws",
        "wss://bot.example.org/ws?access_token=private-token",
        "wss://bot.example.org",
        "wss://bot.example.org/api/auth/login",
        "wss://bot.example.org/../ws",
        "wss://bot.example.org/%2e%2e/ws",
    ],
)
def test_invalid_reverse_ws_env_is_rejected_without_echoing_url(tmp_path, url):
    EnvironmentService(tmp_path).update({REVERSE_WS: url})
    with pytest.raises(EnvironmentError) as caught:
        ConfigService(tmp_path)
    assert url not in str(caught.value)
    assert "private-" not in str(caught.value)


def test_real_nonebot_accepts_env_custom_reverse_path_and_enforces_token(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = Settings()
    settings.web.port = port
    settings.qq.enabled = True
    (tmp_path / "config.yaml").write_text(ConfigService.dump(settings), encoding="utf-8")
    EnvironmentService(tmp_path).update(
        {
            USERNAME: ACCOUNT["username"],
            PASSWORD: ACCOUNT["password"],
            REVERSE_WS: f"ws://127.0.0.1:{port}/soulbot/qq/ws",
        }
    )
    with (tmp_path / ".env").open("a") as stream:
        stream.write("SOULBOT_ONEBOT_ACCESS_TOKEN=isolated-ws-token\n")
    environment = {**os.environ, "SOULBOT_HOME": str(tmp_path)}
    project = Path(__file__).resolve().parents[1]
    with (tmp_path / "runtime.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(project / "main.py")],
            cwd=project,
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:

            async def check():
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", headers=HEADERS
                ) as client:
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            result = await client.get("/api/bootstrap")
                            if result.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        assert process.poll() is None, "Isolated runtime failed to start"
                        assert time.monotonic() < deadline, "Isolated runtime startup timed out"
                        await asyncio.sleep(0.05)
                    assert result.json()["has_admin"]
                    assert (await client.post("/api/auth/login", json=ACCOUNT)).status_code == 200
                    data = (await client.get("/api/config")).json()
                    url = data["qq_reverse_ws_url"]
                    with pytest.raises(InvalidStatus) as rejected:
                        async with websockets.connect(
                            url,
                            additional_headers={
                                "X-Self-ID": "123456789",
                                "Authorization": "Bearer wrong-token",
                            },
                            proxy=None,
                        ):
                            pass
                    assert rejected.value.response.status_code == 403
                    async with websockets.connect(
                        url,
                        additional_headers={
                            "X-Self-ID": "123456789",
                            "Authorization": "Bearer isolated-ws-token",
                        },
                        proxy=None,
                    ) as ws:
                        request = asyncio.create_task(client.post("/api/qq/test"))
                        action = json.loads(await asyncio.wait_for(ws.recv(), 5))
                        assert action["action"] == "get_status"
                        await ws.send(
                            json.dumps(
                                {
                                    "status": "ok",
                                    "retcode": 0,
                                    "data": {"online": True, "good": True},
                                    "echo": action["echo"],
                                }
                            )
                        )
                        assert (await request).json()["ok"]
                    data["config"]["qq"]["enabled"] = False
                    assert (await client.put("/api/config", json=data["config"])).status_code == 200
                    with pytest.raises(InvalidStatus) as disabled:
                        async with websockets.connect(
                            url,
                            additional_headers={
                                "X-Self-ID": "123456790",
                                "Authorization": "Bearer isolated-ws-token",
                            },
                            proxy=None,
                        ):
                            pass
                    assert disabled.value.response.status_code == 403

            asyncio.run(check())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
