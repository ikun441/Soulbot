import asyncio
import json
import wave
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.providers.llm.base import ProviderError
from soulbot.providers.tts.base import SpeechError
from soulbot.services.audio_service import inspect_audio
from soulbot.services.media_service import MediaError
from soulbot.tools.media_tools import specifications
from soulbot.tools.registry import ToolContext


def wav():
    stream = BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0" * 2400)
    return stream.getvalue()


@pytest.fixture
def services(tmp_path):
    return build_services(tmp_path)


def enable(services, monkeypatch):
    services.config.current.tts.enabled = True

    async def synthesize(text, settings):
        return inspect_audio(wav())

    monkeypatch.setattr(services.providers.resolve("tts", "edge"), "synthesize", synthesize)


def test_audio_content_metadata_scope_integrity_expiry_and_path(services):
    audio = inspect_audio(wav())
    assert (audio.format, audio.sample_rate, audio.channels, audio.duration) == (
        "wav",
        24000,
        1,
        0.1,
    )
    item = services.audio.save(audio, scope="private:a")
    assert services.audio.read(item["id"], "private:a") == audio
    assert "scope" not in item and "filename" not in item
    with pytest.raises(MediaError):
        services.audio.read(item["id"], "group:b")
    with pytest.raises(MediaError):
        services.audio.read("../../outside")
    path = services.audio.directory / (item["id"] + ".wav")
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_bytes(b"damaged")
    with pytest.raises(MediaError, match="变化"):
        services.audio.read(item["id"])
    services.audio.clock = lambda: item["expires"] + 1
    services.audio.cleanup()
    assert not path.exists()


@pytest.mark.parametrize("content", [b"", b"not audio", b"RIFF\0\0\0\0WAVE", wav()[:-100]])
def test_audio_rejects_empty_malformed_and_truncated(content):
    with pytest.raises(MediaError):
        inspect_audio(content)


def test_audio_cleanup_refuses_directory_symlink(services, tmp_path):
    services.audio.directory.rmdir()
    outside = tmp_path / "other"
    outside.mkdir()
    services.audio.directory.symlink_to(outside)
    with pytest.raises(MediaError, match="符号链接"):
        services.audio.cleanup()


def test_tts_default_off_and_tool_permissions_scope_and_call_count(services, monkeypatch):
    with pytest.raises(SpeechError, match="未启用"):
        asyncio.run(services.speech.synthesize("你好"))
    assert "synthesize_speech" not in {x["function"]["name"] for x in specifications(services)}
    enable(services, monkeypatch)

    async def check():
        context = ToolContext(scope="private:a")
        result = await services.tools.execute("synthesize_speech", {"text": "你好"}, context)
        key = result["attachment"].split(":")[1]
        assert services.audio.read(key, "private:a").format == "wav"
        with pytest.raises(MediaError):
            services.audio.read(key, "group:b")
        with pytest.raises(PermissionError):
            await services.tools.execute("synthesize_speech", {"text": "again"}, context)
        services.config.current.tools.synthesize_speech = "deny"
        with pytest.raises(PermissionError):
            await services.tools.execute("synthesize_speech", {"text": "denied"}, ToolContext())

    asyncio.run(check())
    assert services.budget.summary(services.config.current)["tts"]["calls"] == 1


def test_persisted_quota_and_cancelled_attempt_is_uncertain(services, monkeypatch, tmp_path):
    enable(services, monkeypatch)
    entered = None

    async def check():
        nonlocal entered
        entered = asyncio.Event()

        async def wait(text, settings):
            entered.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(services.providers.resolve("tts", "edge"), "synthesize", wait)
        task = asyncio.create_task(services.speech.synthesize("取消测试"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not services.speech.jobs

    asyncio.run(check())
    usage = services.budget.summary(services.config.current)["tts"]
    assert usage["recent"][0]["status"] == "uncertain"
    assert usage["recent"][0]["error_kind"] == "cancelled"
    again = build_services(tmp_path)
    again.config.current.tts.enabled = True
    again.config.current.tts.max_calls_per_minute = 1
    with pytest.raises(SpeechError, match="额度"):
        asyncio.run(again.speech.synthesize("不能重置次数"))


def test_speech_and_llm_share_cost_budget_and_unknown_blocks_proactive(services, monkeypatch):
    enable(services, monkeypatch)
    settings = services.config.current
    settings.tts.provider = "openai_compatible"
    settings.tts.compatible.model = "configured-model"
    settings.tts.compatible.voice = "configured-voice"
    services.secrets.save({"tts_api_key": "fixture-not-a-real-key"})
    with pytest.raises(SpeechError, match="未知费用"):
        asyncio.run(services.speech.synthesize("主动", "proactive"))
    settings.tts.compatible.price_cny_per_1000_chars = 100
    settings.behavior.daily_cost_cny = 1
    key = services.speech.reserve(settings, "一二三四五六七八九十", "explicit")
    services.speech.finish(key, "completed")
    with pytest.raises(ProviderError, match="费用预算"):
        services.budget.reserve(settings, settings.llm, "primary", "explicit", 1, 1)
    assert services.budget.summary(settings)["known_or_estimated_cost_cny"] >= 1


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "authentication"),
        (429, "rate_limit"),
        (404, "parameters"),
        (503, "unavailable"),
        (302, "unavailable"),
    ],
)
def test_compatible_status_errors_are_safe_and_never_retried(services, status, kind):
    settings = services.config.current.tts
    settings.compatible.model, settings.compatible.voice = "fixture-model", "fixture-voice"
    services.secrets.save({"tts_api_key": "fixture-private-key"})
    provider = services.providers.resolve("tts", "openai_compatible")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, text="private provider body", headers={"location": "https://other.invalid"}
        )

    provider.transport = httpx.MockTransport(respond)
    with pytest.raises(SpeechError) as error:
        asyncio.run(provider.synthesize("fixture text", settings))
    assert error.value.kind == kind and "private" not in str(error.value)
    assert len(calls) == 1


def test_compatible_contract_independent_key_actual_format_and_capability(services):
    settings = services.config.current.tts
    settings.compatible.model, settings.compatible.voice = "fixture-model", "fixture-voice"
    settings.compatible.response_format = "wav"
    services.secrets.save({"tts_api_key": "fixture-tts-key", "llm_api_key": "must-not-use"})
    provider = services.providers.resolve("tts", "openai_compatible")

    def respond(request):
        assert request.url.path == "/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer fixture-tts-key"
        body = json.loads(request.content)
        assert body["input"] == "fixture text" and body["response_format"] in {"wav", "mp3"}
        assert "instructions" not in body
        return httpx.Response(
            200, content=wav(), headers={"content-type": "application/octet-stream"}
        )

    provider.transport = httpx.MockTransport(respond)

    async def check():
        assert not await provider.health_check()
        result = await provider.synthesize("fixture text", settings)
        assert result.mime == "audio/wav" and result.sample_rate == 24000
        assert await provider.health_check()

    asyncio.run(check())
    settings.compatible.response_format = "mp3"
    with pytest.raises(SpeechError, match="格式"):
        asyncio.run(provider.synthesize("fixture text", settings))


def test_speech_api_auth_preview_no_key_echo_and_no_connected_qq(services, tmp_path, monkeypatch):
    enable(services, monkeypatch)
    with TestClient(
        create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        assert client.get("/api/tts/status").status_code == 401
        assert client.post("/api/tts/synthesize", json={"text": "test"}).status_code == 401
        client.post(
            "/api/auth/setup",
            json={"username": "speech_admin", "password": "speech-test-password-123"},
        )
        services.config.current.tts.enabled = False
        disabled = client.post("/api/tts/synthesize", json={"text": "测试文字"})
        assert disabled.status_code == 422
        assert disabled.json()["kind"] == "disabled" and not disabled.json()["uncertain"]
        services.config.current.tts.enabled = True
        result = client.post("/api/tts/synthesize", json={"text": "测试文字"})
        assert result.status_code == 200
        assert client.get("/api/providers/status").json()["tts"]["state"] == "available"
        item = result.json()
        response = client.get(item["url"])
        assert response.content == wav() and response.headers["content-type"] == "audio/wav"
        services.config.current.personality.owner_qq = "3497689533"
        assert (
            client.post(
                "/api/audio/send-test", json={"confirm": True, "audio_id": item["id"]}
            ).status_code
            == 503
        )

        async def uncertain_failure(text, settings):
            raise SpeechError("timeout", "安全的超时提示", uncertain=True)

        monkeypatch.setattr(
            services.providers.resolve("tts", "edge"), "synthesize", uncertain_failure
        )
        failure = client.post("/api/tts/synthesize", json={"text": "再次测试"})
        assert failure.status_code == 422
        assert failure.json()["kind"] == "timeout" and failure.json()["uncertain"]
        assert "api_key" not in client.get("/api/tts/status").text


def test_voice_disclosure_is_separate_and_permission_rechecked_before_record(services, monkeypatch):
    enable(services, monkeypatch)
    services.config.current.qq.enabled = True
    event = ChatEvent("12345", "3497689533", "voice-test", "", directed=True)
    executor = services.agent.executor
    monkeypatch.setattr(executor.decision, "reserve_send", lambda settings: 0)
    sent = []

    async def sender(event, payload):
        sent.append(payload)

    executor.sender = sender
    assert asyncio.run(executor.send_audio(event, inspect_audio(wav()), "admin"))
    assert sent[0] == "【AI 合成语音】" and sent[1].format == "wav"
    sent.clear()

    async def revoke(event, payload):
        sent.append(payload)
        services.config.current.tts.enabled = False

    executor.sender = revoke
    assert not asyncio.run(executor.send_audio(event, inspect_audio(wav()), "admin"))
    assert sent == ["【AI 合成语音】"]


def test_schema_four_migration_preserves_account_and_usage(services, tmp_path):
    services.db.set("fixture", "preserve")
    with services.db.connect() as db:
        db.execute("DROP TABLE audio_artifacts")
        db.execute("DROP TABLE tts_usage")
        db.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
    again = build_services(tmp_path)
    assert again.db.get("fixture") == "preserve"
    assert again.db.get("schema_version") == "11"


def test_concurrent_speech_is_bounded_and_shutdown_cancels_jobs(services, monkeypatch):
    enable(services, monkeypatch)

    async def check():
        entered = asyncio.Event()

        async def wait(text, settings):
            entered.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(services.providers.resolve("tts", "edge"), "synthesize", wait)
        first = asyncio.create_task(services.speech.synthesize("one"))
        await entered.wait()
        second = asyncio.create_task(services.speech.synthesize("two"))
        await asyncio.sleep(0)
        with pytest.raises(SpeechError, match="任务较多"):
            await services.speech.synthesize("three")
        await services.speech.close()
        assert first.cancelled() and second.cancelled() and not services.speech.jobs

    asyncio.run(check())
    assert all(
        row["status"] == "uncertain"
        for row in services.budget.summary(services.config.current)["tts"]["recent"]
    )


def test_timeout_keeps_reservation_and_safe_error(services, monkeypatch):
    enable(services, monkeypatch)

    async def timeout(text, settings):
        raise TimeoutError("private request")

    monkeypatch.setattr(services.providers.resolve("tts", "edge"), "synthesize", timeout)
    with pytest.raises(SpeechError, match="可能已产生费用"):
        asyncio.run(services.speech.synthesize("timeout"))
    row = services.budget.summary(services.config.current)["tts"]["recent"][0]
    assert row["status"] == "uncertain" and row["error_kind"] == "timeout"


def test_edge_voice_cache_uses_live_response_and_rejects_unknown_voice(services, monkeypatch):
    import edge_tts

    provider = services.providers.resolve("tts", "edge")
    calls = []

    async def voices():
        calls.append(True)
        return [{"ShortName": "fixture-Neural", "Locale": "zh-CN", "Gender": "Female"}]

    monkeypatch.setattr(edge_tts, "list_voices", voices)

    async def check():
        assert (await provider.voices())[0]["id"] == "fixture-Neural"
        await provider.voices()
        with pytest.raises(SpeechError, match="实际列表"):
            await provider.synthesize("voice", services.config.current.tts)
        assert len(calls) == 1

    asyncio.run(check())


def test_onebot_audio_is_standalone_record_with_actual_receipt(services, monkeypatch):
    import nonebot

    from soulbot.adapters.onebot_v11 import NoneBotTransport

    messages = []

    class Bot:
        async def send_private_msg(self, **kwargs):
            messages.append(kwargs["message"])
            return {"message_id": 123}

    monkeypatch.setattr(nonebot, "get_bot", lambda bot_id: Bot())
    event = ChatEvent("12345", "3497689533", "voice-test", "")
    asyncio.run(NoneBotTransport()(event, inspect_audio(wav())))
    assert messages[0].type == "record"
    assert messages[0].data["file"].startswith("base64://UklGR")


def test_pending_tts_is_preserved_as_uncertain_after_restart(services, tmp_path):
    key = services.speech.reserve(services.config.current, "pending", "explicit")
    again = build_services(tmp_path)
    row = again.budget.summary(again.config.current)["tts"]["recent"][0]
    assert row["id"] == key and row["status"] == "uncertain" and row["error_kind"] == "interrupted"
