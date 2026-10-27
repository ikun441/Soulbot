import asyncio
import base64
import sqlite3
import wave
from dataclasses import replace
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.providers.asr.base import ASRError, Transcription
from soulbot.providers.llm.base import ProviderError
from soulbot.services.audio_service import MAX_AUDIO_BYTES, inspect_audio
from soulbot.services.config_service import ASRSettings, Settings
from soulbot.services.media_service import MediaError


def wav(seconds=0.1):
    stream = BytesIO()
    with wave.open(stream, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(24000)
        file.writeframes(bytes(int(seconds * 24000) * 2))
    return stream.getvalue()


@pytest.fixture
def services(tmp_path):
    return build_services(tmp_path)


def enabled(services, monkeypatch):
    services.config.current.asr.enabled = True
    monkeypatch.setattr(services.recognition.local, "ready", lambda: True)

    async def transcribe(audio, settings):
        return Transcription("私有测试文字", "zh", audio.duration)

    monkeypatch.setattr(services.recognition.local, "transcribe", transcribe)


@pytest.mark.parametrize(
    "values",
    [
        {"language": "../../file"},
        {"max_duration_seconds": 181},
        {"timeout_seconds": float("nan")},
        {"base_url": "file:///tmp"},
        {"price_cny_per_minute": -1},
    ],
)
def test_asr_config_validates_language_url_bounds_and_price(values):
    with pytest.raises(ValidationError):
        ASRSettings.model_validate(values)


def test_qq_asr_requires_reverse_ws_for_large_base64_response():
    with pytest.raises(ValidationError, match="反向 WebSocket"):
        Settings.model_validate({"qq": {"connection": "forward"}, "asr": {"qq_enabled": True}})


def test_asr_disabled_unprepared_and_invalid_audio_do_not_reserve(services, monkeypatch):
    with pytest.raises(ASRError, match="未启用"):
        asyncio.run(services.recognition.transcribe(wav()))
    services.config.current.asr.enabled = True
    with pytest.raises(ASRError, match="准备本地模型"):
        asyncio.run(services.recognition.transcribe(wav()))
    enabled(services, monkeypatch)
    for content in [b"bad", b"", bytes(MAX_AUDIO_BYTES + 1), wav(2)]:
        services.config.current.asr.max_duration_seconds = 1
        with pytest.raises(ASRError):
            asyncio.run(services.recognition.transcribe(content))
    assert services.budget.summary(services.config.current)["asr"]["calls"] == 0


def test_asr_api_auth_actual_content_and_no_persisted_transcript(services, tmp_path, monkeypatch):
    enabled(services, monkeypatch)
    with TestClient(
        create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        assert client.get("/api/asr/status").status_code == 401
        assert (
            client.post("/api/asr/transcribe", files={"file": ("x.wav", wav())}).status_code == 401
        )
        client.post(
            "/api/auth/setup", json={"username": "asr_fixture", "password": "asr-test-password-123"}
        )
        services.config.current.asr.enabled = False
        disabled = client.post("/api/asr/transcribe", files={"file": ("x.wav", wav())})
        assert disabled.status_code == 422
        assert disabled.json()["kind"] == "disabled" and not disabled.json()["uncertain"]
        services.config.current.asr.enabled = True
        result = client.post(
            "/api/asr/transcribe", files={"file": ("misleading.mp3", wav(), "audio/mpeg")}
        )
        assert result.status_code == 200 and result.json()["text"] == "私有测试文字"
        assert result.json()["duration"] == 0.1
        assert client.get("/api/providers/status").json()["asr"]["state"] == "available"

        async def uncertain_failure(audio, settings):
            raise ASRError("timeout", "安全的识别超时提示", uncertain=True)

        monkeypatch.setattr(services.recognition.local, "transcribe", uncertain_failure)
        failure = client.post("/api/asr/transcribe", files={"file": ("uncertain.wav", wav())})
        assert failure.status_code == 422
        assert failure.json()["kind"] == "timeout" and failure.json()["uncertain"]
        assert (
            client.post("/api/asr/transcribe", files={"file": ("x.wav", b"bad")}).status_code == 422
        )
        usage = client.get("/api/asr/status").json()["usage"]
        assert usage["calls"] == 2
        assert usage["recent"][0]["status"] == "uncertain"
        assert usage["recent"][0]["error_kind"] == "timeout"
        assert "私有测试文字" not in client.get("/api/audit").text
        assert "私有测试文字" not in client.get("/api/usage").text
        assert "api_key" not in client.get("/api/asr/status").text
        assert not list((tmp_path / "data" / "audio").iterdir())


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limit"),
        (404, "parameters"),
        (503, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_cloud_errors_no_retry_no_redirect_no_private_error_body(services, status, kind):
    services.secrets.save({"asr_api_key": "fixture-asr-key"})
    settings = services.config.current.asr
    settings.model = "fixture-asr-model"
    provider = services.providers.resolve("asr", "openai_compatible")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, text="private provider response", headers={"location": "https://other.invalid"}
        )

    provider.transport = httpx.MockTransport(respond)
    with pytest.raises(ASRError) as error:
        asyncio.run(provider.transcribe(inspect_audio(wav()), settings))
    assert error.value.kind == kind and "private" not in str(error.value)
    assert len(calls) == 1


def test_cloud_multipart_contract_uses_independent_key_and_validated_duration(services):
    services.secrets.save(
        {
            "asr_api_key": "fixture-asr-key",
            "tts_api_key": "must-not-use",
            "llm_api_key": "must-not-use",
        }
    )
    settings = services.config.current.asr
    settings.model = "fixture-asr-model"
    provider = services.providers.resolve("asr", "openai_compatible")

    def respond(request):
        assert request.url.path == "/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer fixture-asr-key"
        assert "multipart/form-data" in request.headers["content-type"]
        assert b'filename="audio.wav"' in request.content and wav() in request.content
        assert b'name="model"' in request.content and b"fixture-asr-model" in request.content
        assert b'name="response_format"' in request.content and b"json" in request.content
        return httpx.Response(200, json={"text": "识别文字", "duration": -999})

    provider.transport = httpx.MockTransport(respond)
    result = asyncio.run(provider.transcribe(inspect_audio(wav()), settings))
    assert result.text == "识别文字" and result.duration == 0.1


@pytest.mark.parametrize("response", [{}, {"text": 42}, {"text": "x" * 12001}, ["not an object"]])
def test_cloud_invalid_response_is_uncertain(services, response):
    services.secrets.save({"asr_api_key": "fixture-asr-key"})
    services.config.current.asr.model = "fixture-model"
    provider = services.providers.resolve("asr", "openai_compatible")
    provider.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    with pytest.raises(ASRError) as error:
        asyncio.run(provider.transcribe(inspect_audio(wav()), services.config.current.asr))
    assert error.value.kind == "format" and error.value.uncertain


def test_asr_quota_persists_and_shared_budget_blocks_llm_and_tts(services, tmp_path):
    settings = services.config.current
    settings.asr.provider = "openai_compatible"
    settings.asr.price_cny_per_minute = 60
    settings.behavior.daily_cost_cny = 1
    key = services.recognition.reserve(settings, 1, "fixture")
    services.recognition.finish(key, "completed")
    with pytest.raises(ProviderError, match="费用预算"):
        services.budget.reserve(settings, settings.llm, "primary", "explicit", 1, 1)
    with pytest.raises(Exception, match="费用预算"):
        services.speech.reserve(settings, "tts", "explicit")
    again = build_services(tmp_path)
    again.config.current.asr.max_calls_per_minute = 1
    with pytest.raises(ASRError, match="额度"):
        again.recognition.reserve(again.config.current, 0.1, "fixture")
    assert again.budget.summary(again.config.current)["asr"]["seconds"] == 1


def test_unknown_asr_price_blocks_proactive_llm(services):
    settings = services.config.current
    settings.asr.provider = "openai_compatible"
    key = services.recognition.reserve(settings, 1, "fixture")
    services.recognition.finish(key, "completed")
    settings.llm.input_price_cny_per_million = 0
    settings.llm.output_price_cny_per_million = 0
    with pytest.raises(ProviderError, match="未知费用"):
        services.budget.reserve(settings, settings.llm, "primary", "proactive", 1, 1)
    assert services.budget.summary(settings)["unknown_cost_calls"] == 1


def test_cancel_concurrency_shutdown_and_restart_preserve_uncertain_seconds(
    services, monkeypatch, tmp_path
):
    enabled(services, monkeypatch)

    async def check():
        entered = asyncio.Event()

        async def wait(audio, settings):
            entered.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(services.recognition.local, "transcribe", wait)
        task = asyncio.create_task(services.recognition.transcribe(wav()))
        await entered.wait()
        with pytest.raises(ASRError, match="任务较多"):
            await services.recognition.transcribe(wav())
        await services.recognition.close()
        assert task.cancelled() and not services.recognition.jobs

    asyncio.run(check())
    again = build_services(tmp_path)
    row = again.budget.summary(again.config.current)["asr"]["recent"][0]
    assert (
        row["status"] == "uncertain" and row["error_kind"] == "cancelled" and row["seconds"] == 0.1
    )


def test_local_timeout_terminates_owned_worker(services, monkeypatch, tmp_path):
    import soulbot.providers.asr.local as module

    worker = tmp_path / "slow-worker.py"
    worker.write_text("import time; time.sleep(30)")
    monkeypatch.setattr(module, "WORKER", worker)
    services.config.current.asr.enabled = True
    services.config.current.asr.timeout_seconds = 0.05
    monkeypatch.setattr(services.recognition.local, "ready", lambda: True)
    with pytest.raises(ASRError, match="超时"):
        asyncio.run(services.recognition.transcribe(wav()))
    assert not services.recognition.local.processes and not services.recognition.jobs


def test_local_prepare_cancel_releases_lock_and_environment_has_no_keys(
    services, monkeypatch, tmp_path
):
    monkeypatch.setenv("SOULBOT_ASR_API_KEY", "fixture-private-key")
    local = services.recognition.local
    environment = local.environment()
    assert "SOULBOT_ASR_API_KEY" not in environment and environment["HF_HUB_OFFLINE"] == "1"

    async def wait(*args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(local, "command", wait)

    async def check():
        await local.begin_prepare()
        await asyncio.sleep(0)
        await local.cancel_prepare()
        from soulbot.services.meme_service import InstanceLock

        other = InstanceLock(local.root / "prepare.lock")
        assert other.acquire()
        other.close()

    asyncio.run(check())


def test_local_model_symlink_is_rejected(services, tmp_path):
    local = services.recognition.local
    outside = tmp_path / "outside"
    outside.mkdir()
    local.root.parent.mkdir(exist_ok=True)
    local.root.symlink_to(outside)
    assert not local.ready()
    with pytest.raises(ASRError, match="符号链接"):
        asyncio.run(local.begin_prepare())


def test_prepare_cancel_before_first_task_turn_releases_lock(services):
    from soulbot.services.meme_service import InstanceLock

    async def check():
        local = services.recognition.local
        await local.begin_prepare()
        await local.cancel_prepare()
        other = InstanceLock(local.root / "prepare.lock")
        assert other.acquire()
        other.close()

    asyncio.run(check())


def test_napcat_record_fetch_uses_opaque_id_base64_and_no_file_url(services, monkeypatch):
    import nonebot

    from soulbot.adapters.onebot_v11 import NoneBotTransport

    calls = []

    class Bot:
        async def call_api(self, action, **parameters):
            calls.append((action, parameters))
            return {
                "base64": base64.b64encode(wav()).decode(),
                "file": "/must-not-read",
                "url": "http://127.0.0.1/private",
            }

    monkeypatch.setattr(nonebot, "get_bot", lambda bot_id: Bot())
    transport = NoneBotTransport()
    event = ChatEvent("12345", "3497689533", "fixture", "", record_file="1234abcd")
    assert asyncio.run(transport.fetch_record(event)) == wav()
    assert calls == [("get_record", {"file_id": "1234abcd", "out_format": "wav"})]
    for value in ["../../private", "file:///tmp/a", "https://internal.invalid", "", "a" * 200]:
        with pytest.raises(MediaError):
            asyncio.run(transport.fetch_record(replace(event, record_file=value)))
    assert len(calls) == 1


def test_qq_record_prefilter_dedupe_cooldown_and_transcript_is_user_content(services, monkeypatch):
    enabled(services, monkeypatch)
    services.config.current.qq.enabled = True
    services.config.current.asr.qq_enabled = True
    fetched, queued = [], []

    class Transport:
        async def fetch_record(self, event):
            fetched.append(event)
            return wav()

    async def enqueue(event, trigger):
        queued.append((event, trigger))

    services.qq_transport = Transport()
    monkeypatch.setattr(services.agent, "enqueue", enqueue)
    event = ChatEvent("12345", "3497689533", "voice-1", "", group_id="54321", record_file="abcd")

    async def check():
        await services.agent.accept_record(event)
        assert not fetched
        directed = replace(event, directed=True)
        await services.agent.accept_record(directed)
        await services.agent.accept_record(directed)
        await services.agent.accept_record(replace(directed, message_id="voice-2"))
        assert len(fetched) == len(queued) == 1
        converted, trigger = queued[0]
        assert converted.scope == event.scope and converted.record_file is None
        assert converted.text == "[语音转写，可能有误] 私有测试文字" and trigger == "direct"
        assert services.agent.claim_record(replace(directed, group_id="different-group"))

    asyncio.run(check())


def test_qq_revoke_during_fetch_does_not_transcribe(services, monkeypatch):
    enabled(services, monkeypatch)
    services.config.current.qq.enabled = True
    services.config.current.asr.qq_enabled = True

    class Transport:
        async def fetch_record(self, event):
            services.config.current.asr.qq_enabled = False
            return wav()

    services.qq_transport = Transport()
    event = ChatEvent("12345", "3497689533", "voice-1", "", record_file="abcd")
    asyncio.run(services.agent.accept_record(event))
    assert services.budget.summary(services.config.current)["asr"]["calls"] == 0
    assert not services.agent.record_jobs and not services.agent.pending


def test_schema_five_migration_preserves_account_and_tts_and_pending_asr(services, tmp_path):
    services.auth.setup("asr_admin", "migration-test-password-123")
    token = services.auth.session()
    key = services.speech.reserve(services.config.current, "existing", "fixture")
    with services.db.connect() as db:
        admin = tuple(db.execute("SELECT * FROM admins").fetchone())
        db.execute("DROP TABLE asr_usage")
        db.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
    again = build_services(tmp_path)
    assert again.auth.valid(token) and again.db.get("schema_version") == "10"
    with sqlite3.connect(again.db.path) as db:
        assert db.execute("SELECT * FROM admins").fetchone() == admin
        assert db.execute("SELECT id FROM tts_usage").fetchone()[0] == key
    pending = again.recognition.reserve(again.config.current, 0.1, "fixture")
    third = build_services(tmp_path)
    row = third.budget.summary(third.config.current)["asr"]["recent"][0]
    assert (
        row["id"] == pending and row["status"] == "uncertain" and row["error_kind"] == "interrupted"
    )
