"""Live Edge synthesis through authenticated APIs and scoped tools; never sends QQ."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.services.media_service import MediaError
from soulbot.tools.registry import ToolContext

ROOT = Path(__file__).resolve().parents[1]


def main():
    root = ROOT / "output" / "tts-live" / "acceptance"
    services = build_services(root)
    settings = services.config.current.model_copy(deep=True)
    settings.tts.enabled = True
    services.config.save(settings)
    with TestClient(create_web_app(root, services), headers={"X-Soulbot-Request": "1"}) as client:
        account = {"username": "speech_acceptance", "password": "isolated-voice-password-123"}
        endpoint = (
            "/api/auth/login"
            if client.get("/api/bootstrap").json()["has_admin"]
            else "/api/auth/setup"
        )
        assert client.post(endpoint, json=account).status_code == 200
        voices = client.get("/api/tts/voices")
        assert voices.status_code == 200, voices.text
        assert any(item["id"] == settings.tts.edge.voice for item in voices.json()["items"])
        response = client.post(
            "/api/tts/synthesize", json={"text": "你好，这是 SoulBot 的人工智能合成语音测试。"}
        )
        assert response.status_code == 200, response.text
        artifact = response.json()
        assert artifact["format"] == "mp3" and artifact["sample_rate"] == 24000
        payload = client.get(artifact["url"])
        assert payload.status_code == 200 and payload.headers["content-type"] == "audio/mpeg"
        assert len(payload.content) == artifact["size"]
        context = ToolContext(scope="private:voice-acceptance")
        result = client.portal.call(
            services.tools.execute,
            "synthesize_speech",
            {"text": "这是带会话权限的真实工具语音。"},
            context,
        )
        key = result["attachment"].split(":", 1)[1]
        assert services.audio.read(key, context.scope).format == "mp3"
        try:
            services.audio.read(key, "group:unrelated")
        except MediaError:
            pass
        else:
            raise AssertionError("Audio leaked across scopes")
        usage = client.get("/api/usage").json()["tts"]
        results = {
            "voice_count": len(voices.json()["items"]),
            "voice": settings.tts.edge.voice,
            "artifact": artifact,
            "authenticated_preview": True,
            "real_tool_scope_isolation": True,
            "usage": usage,
            "qq_live_test": "not_run",
            "compatible_cloud_live_test": "not_run",
        }
        root.parent.joinpath("result.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
