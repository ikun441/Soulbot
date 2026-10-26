from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.services.provider_diagnostics import ProviderDiagnostics


def test_status_only_reports_matching_settings_and_forgets_disabled_result():
    diagnostics = ProviderDiagnostics()
    settings = {"provider": "fixture", "model": "one", "enabled": True}
    secret = "private-provider-secret"
    diagnostics.record("fixture", settings, secret, ok=True)

    status = diagnostics.status("fixture", settings, secret, enabled=True, configured=True)
    assert status["state"] == "available" and status["last_test_at"]
    assert secret not in str(status)

    changed = {**settings, "model": "two"}
    status = diagnostics.status("fixture", changed, secret, enabled=True, configured=True)
    assert status == {"state": "not_tested", "last_test_at": None, "last_error_kind": None}
    diagnostics.status("fixture", changed, secret, enabled=False, configured=True)
    assert diagnostics.status("fixture", settings, secret, enabled=True, configured=True)[
        "state"
    ] == ("not_tested")


def test_provider_status_api_is_protected_and_does_not_return_secrets(tmp_path):
    services = build_services(tmp_path)
    services.secrets.save({"llm_api_key": "api-secret-fixture"})
    with TestClient(
        create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        assert client.get("/api/providers/status").status_code == 401
        client.post(
            "/api/auth/setup",
            json={"username": "diagnostic_admin", "password": "diagnostic-password-123"},
        )
        response = client.get("/api/providers/status")
        assert response.status_code == 200
        statuses = response.json()
        assert set(statuses) == {"llm", "tts", "asr", "image_generation"}
        assert statuses["llm"]["state"] == "unconfigured"
        assert statuses["tts"]["state"] == "disabled"
        assert statuses["asr"]["state"] == "disabled"
        assert statuses["image_generation"]["state"] == "disabled"
        assert "api-secret-fixture" not in response.text
