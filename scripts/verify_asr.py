"""Real local-model transcription via authenticated upload; no QQ or cloud requests."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.services.meme_service import InstanceLock

ROOT = Path(__file__).resolve().parents[1]


def main():
    root = ROOT / "output" / "asr-live"
    services = build_services(root)
    local = services.recognition.local
    local.root = ROOT / "data" / "asr"
    local.directory = local.root / "base"
    local.lock = InstanceLock(local.root / "prepare.lock")
    if not local.ready():
        raise SystemExit("Prepare local model in WebUI first")
    settings = services.config.current.model_copy(deep=True)
    settings.asr.enabled = True
    settings.asr.provider = "local"
    services.config.save(settings)
    source = ROOT / "output" / "tts-live" / "edge-probe.mp3"
    if not source.is_file():
        previous = ROOT / "output" / "tts-live" / "result.json"
        if previous.is_file():
            key = json.loads(previous.read_text()).get("artifact", {}).get("id", "")
            import re

            if re.fullmatch(r"[a-f0-9]{32}", key):
                source = previous.parent / "acceptance" / "data" / "audio" / (key + ".mp3")
    if not source.is_file():
        raise SystemExit("Generate a non-private Edge test MP3 first; see verify_tts.py")
    with TestClient(create_web_app(root, services), headers={"X-Soulbot-Request": "1"}) as client:
        account = {"username": "asr_acceptance", "password": "isolated-asr-password-123"}
        endpoint = (
            "/api/auth/login"
            if client.get("/api/bootstrap").json()["has_admin"]
            else "/api/auth/setup"
        )
        assert client.post(endpoint, json=account).status_code == 200
        assert client.get("/api/asr/status").json()["local_ready"]
        response = client.post(
            "/api/asr/transcribe", files={"file": ("test.mp3", source.read_bytes(), "audio/mpeg")}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["text"].strip() and result["duration"] > 0 and result["language"] == "zh"
        assert not local.processes and not services.recognition.jobs
        usage = client.get("/api/usage").json()["asr"]
        with services.db.connect() as db:
            assert "text" not in [row[1] for row in db.execute("PRAGMA table_info(asr_usage)")]
        output = {
            "provider": "local_cpu_int8",
            "model_revision": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
            "source": "generic Edge test audio",
            "authenticated_upload": True,
            "result": result,
            "usage": usage,
            "owned_process_cleanup": True,
            "cloud_live_test": "not_run",
            "qq_live_test": "not_run",
        }
        (root / "result.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
