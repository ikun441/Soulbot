"""Voice routes use isolated providers; fixtures never claim real cloud verification."""

import asyncio
import json
import wave
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.providers.asr.base import ASRError, Transcription
from soulbot.providers.tts.base import SpeechError
from soulbot.services.audio_service import inspect_audio
from soulbot.services.backup_service import BackupService, InstanceLock
from soulbot.services.config_service import ASREndpoint, ASRSettings, SpeechEndpoint, SpeechSettings
from soulbot.storage.database import Database


def wav():
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 800)
    return output.getvalue()


@pytest.fixture(params=["tts", "asr"])
def s(tmp_path, request):
    services = build_services(tmp_path)
    kind = request.param
    service = services.speech if kind == "tts" else services.recognition
    cfg = getattr(services.config.current, kind)
    cfg.enabled = True
    if kind == "asr":
        service.local.ready = lambda: True
        cfg.allow_cloud_fallback = True
        cfg.fallbacks["fallback_1"] = ASREndpoint(
            enabled=True,
            provider="openai_compatible",
            base_url="https://backup.example/v1",
            model="fixture-model",
            price_cny_per_minute=3,
        )
    else:
        cfg.fallbacks["fallback_1"] = SpeechEndpoint(
            enabled=True,
            provider="openai_compatible",
            compatible={
                "base_url": "https://backup.example/v1",
                "model": "fixture-model",
                "voice": "fixture-voice",
                "response_format": "wav",
                "price_cny_per_1000_chars": 2,
            },
        )
    services.secrets.save(
        {kind + "_api_key": "primary-fixture-key", kind + "_fallback_1": "backup-fixture-key"}
    )
    s = SimpleNamespace(
        services=services,
        kind=kind,
        service=service,
        cfg=cfg,
        error=SpeechError if kind == "tts" else ASRError,
        calls=[],
    )
    primary = services.providers.resolve(kind, "edge" if kind == "tts" else "local")

    async def fail(value, policy):
        s.calls.append("primary")
        raise s.error("network", "local fixture failure", True)

    setattr(primary, "synthesize" if kind == "tts" else "transcribe", fail)
    s.primary = primary

    def handler(request):
        s.calls.append("fallback_1")
        assert request.headers["authorization"] == "Bearer backup-fixture-key"
        assert request.url.host == "backup.example"
        if kind == "tts":
            assert request.url.path == "/v1/audio/speech"
            assert json.loads(request.content)["voice"] == "fixture-voice"
            return httpx.Response(200, content=wav())
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"fixture-model" in request.content and b"RIFF" in request.content
        return httpx.Response(200, json={"text": "明确标识的本地测试结果"})

    services.providers.resolve(kind, "fallback_1").transport = httpx.MockTransport(handler)
    return s


def run(s, **kwargs):
    if s.kind == "tts":
        return asyncio.run(s.service.synthesize("公开测试文字", **kwargs))
    return asyncio.run(s.service.transcribe(wav(), **kwargs))


def rows(s):
    with s.services.db.connect() as db:
        return [
            dict(r)
            for r in db.execute(
                f"SELECT u.*,a.route_id,a.request_id FROM {s.kind}_usage u LEFT JOIN media_attempts a ON a.usage_id=u.id ORDER BY u.rowid"
            )
        ]


def test_fallback_uses_independent_credentials_and_keeps_each_attempt(s):
    result = run(s)
    assert result.provider_id == "fallback_1"
    assert s.calls == ["primary", "fallback_1"]
    data = rows(s)
    assert [r["status"] for r in data] == ["uncertain", "completed"]
    assert [r["provider"] for r in data] == [
        "edge" if s.kind == "tts" else "local",
        "openai_compatible",
    ]
    assert [r["route_id"] for r in data] == ["primary", "fallback_1"]
    assert data[0]["request_id"] == data[1]["request_id"]
    assert data[0]["cost_cny"] == 0 and data[1]["cost_cny"] > 0
    assert s.service.router.status()[0]["state"] == "failed"
    assert s.service.router.status()[1]["state"] == "available"
    assert s.service.test_status()["state"] == "available"
    assert (
        s.services.budget.summary(s.services.config.current)[s.kind]["recent"][0]["route_id"]
        == "fallback_1"
    )


def test_valid_primary_result_stops_and_empty_asr_does_not_switch(s):
    async def success(value, policy):
        s.calls.append("primary")
        return inspect_audio(wav()) if s.kind == "tts" else Transcription("", None, 0.1)

    setattr(s.primary, "synthesize" if s.kind == "tts" else "transcribe", success)
    assert run(s).provider_id == "primary"
    assert s.calls == ["primary"] and len(rows(s)) == 1


def test_only_target_never_falls_back_when_primary_fails(s):
    with pytest.raises(s.error):
        run(s, only="primary")
    assert s.calls == ["primary"] and len(rows(s)) == 1
    assert run(s, only="fallback_1").provider_id == "fallback_1"
    assert s.calls == ["primary", "fallback_1"]


def test_unknown_or_disabled_target_never_sends(s):
    for route in ["other", "fallback_2"]:
        with pytest.raises(s.error):
            run(s, only=route)
    s.cfg.fallbacks["fallback_1"].enabled = False
    with pytest.raises(s.error):
        run(s, only="fallback_1")
    assert not s.calls and not rows(s)


def test_unconfigured_primary_is_skipped_without_billable_reservation(s):
    if s.kind == "tts":
        s.cfg.provider = "openai_compatible"
        s.cfg.compatible.model = ""
    else:
        s.service.local.ready = lambda: False
    assert run(s).provider_id == "fallback_1"
    assert s.calls == ["fallback_1"] and len(rows(s)) == 1


def test_unknown_backup_price_is_not_displayed_as_free(s):
    if s.kind == "tts":
        s.cfg.fallbacks["fallback_1"].compatible.price_cny_per_1000_chars = None
    else:
        s.cfg.fallbacks["fallback_1"].price_cny_per_minute = None
    run(s)
    assert rows(s)[1]["cost_cny"] is None


@pytest.mark.parametrize("change", ["config", "key", "disable", "pause"])
def test_slow_failure_rechecks_config_keys_and_global_pause_before_next_attempt(s, change):
    async def fail(value, policy):
        s.calls.append("primary")
        if change == "config":
            s.cfg.fallbacks["fallback_1"].timeout_seconds = 13
        elif change == "key":
            s.services.secrets.save({s.kind + "_fallback_1": "new-private-fixture-key"})
        elif change == "disable":
            s.cfg.enabled = False
        else:
            s.services.config.current.automation.paused = True
        raise s.error("network", "local fixture failure", True)

    setattr(s.primary, "synthesize" if s.kind == "tts" else "transcribe", fail)
    with pytest.raises(s.error):
        run(s, purpose="proactive_draft")
    assert s.calls == ["primary"] and len(rows(s)) == 1


def test_cancel_never_switches_and_keeps_unknown_consumption(s):
    async def scenario():
        entered = asyncio.Event()

        async def wait(value, policy):
            s.calls.append("primary")
            entered.set()
            await asyncio.Event().wait()

        setattr(s.primary, "synthesize" if s.kind == "tts" else "transcribe", wait)
        method = s.service.synthesize if s.kind == "tts" else s.service.transcribe
        task = asyncio.create_task(method("公开测试文字" if s.kind == "tts" else wav()))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert s.calls == ["primary"] and not s.service.jobs
    assert rows(s)[0]["error_kind"] == "cancelled" and rows(s)[0]["status"] == "uncertain"


def test_total_timeout_does_not_reserve_another_service(s):
    s.cfg.total_timeout_seconds = 0.02

    async def wait(value, policy):
        s.calls.append("primary")
        await asyncio.sleep(1)

    setattr(s.primary, "synthesize" if s.kind == "tts" else "transcribe", wait)
    with pytest.raises(s.error) as error:
        run(s)
    assert error.value.kind == "timeout" and s.calls == ["primary"]
    assert len(rows(s)) == 1 and rows(s)[0]["error_kind"] == "timeout"


def test_provider_timeout_with_remaining_total_time_can_fall_back(s):
    s.cfg.timeout_seconds = 0.02

    async def wait(value, policy):
        s.calls.append("primary")
        await asyncio.sleep(1)

    setattr(s.primary, "synthesize" if s.kind == "tts" else "transcribe", wait)
    assert run(s).provider_id == "fallback_1"
    assert s.calls == ["primary", "fallback_1"]


def test_output_or_input_safety_error_does_not_retry(s):
    async def fail(value, policy):
        s.calls.append("primary")
        raise s.error("size", "oversized local fixture", True)

    setattr(s.primary, "synthesize" if s.kind == "tts" else "transcribe", fail)
    with pytest.raises(s.error):
        run(s)
    assert s.calls == ["primary"] and len(rows(s)) == 1


def test_quota_counts_backup_attempts(s):
    s.cfg.max_calls_per_minute = 1
    with pytest.raises(s.error) as error:
        run(s)
    assert error.value.kind == "quota" and s.calls == ["primary"]
    assert len(rows(s)) == 1


def test_missing_backup_key_is_never_replaced_by_primary_or_chat_key(s):
    s.services.secrets.save({s.kind + "_fallback_1": "", "llm_api_key": "must-not-use-chat-key"})
    with pytest.raises(s.error):
        run(s)
    assert s.calls == ["primary"] and len(rows(s)) == 1
    assert s.service.router.status()[1]["state"] == "unconfigured"


def test_secret_changes_invalidate_only_affected_endpoint(s):
    run(s)
    assert s.service.router.status()[1]["state"] == "available"
    s.services.secrets.save({s.kind + "_fallback_1": "replacement-fixture"})
    assert s.service.router.status()[1]["state"] == "not_tested"
    assert s.service.test_status()["state"] == "not_tested"


def test_shutdown_cancels_active_attempt_and_blocks_new_jobs(s):
    async def scenario():
        entered = asyncio.Event()

        async def wait(value, policy):
            s.calls.append("primary")
            entered.set()
            await asyncio.Event().wait()

        setattr(s.primary, "synthesize" if s.kind == "tts" else "transcribe", wait)
        method = s.service.synthesize if s.kind == "tts" else s.service.transcribe
        task = asyncio.create_task(method("公开测试文字" if s.kind == "tts" else wav()))
        await asyncio.wait_for(entered.wait(), 2)
        await s.service.close()
        assert task.cancelled()
        with pytest.raises(s.error):
            await method("公开测试文字" if s.kind == "tts" else wav())

    asyncio.run(scenario())
    assert s.calls == ["primary"] and rows(s)[0]["error_kind"] == "cancelled"


def test_http_api_selects_single_service_and_does_not_return_secrets(s):
    headers = {"X-Soulbot-Request": "1"}
    with TestClient(create_web_app(s.services.root, s.services), headers=headers) as c:
        assert c.get("/api/" + s.kind + "/status").status_code == 401
        c.post(
            "/api/auth/setup",
            json={"username": "tester", "password": "voice-route-fixture-password"},
        ).raise_for_status()
        if s.kind == "tts":
            response = c.post(
                "/api/tts/synthesize", json={"text": "公开测试文字", "provider_id": "fallback_1"}
            )
        else:
            response = c.post(
                "/api/asr/transcribe?provider_id=fallback_1", files={"file": ("fixture.wav", wav())}
            )
        assert response.status_code == 200 and response.json()["provider_id"] == "fallback_1"
        assert s.calls == ["fallback_1"]
        status = c.get("/api/" + s.kind + "/status").json()
        assert status["routes"][1]["state"] == "available"
        assert "backup-fixture-key" not in json.dumps(status)
        assert "primary-fixture-key" not in c.get("/api/config").text
        invalid = (
            c.post("/api/tts/synthesize", json={"text": "公开测试文字", "provider_id": "other"})
            if s.kind == "tts"
            else c.post("/api/asr/transcribe?provider_id=other", files={"file": ("x.wav", wav())})
        )
        assert invalid.status_code == 422 and len(s.calls) == 1


def test_restore_keeps_newer_attempts_costs_and_quota(s):
    s.services.config.save(s.services.config.current)
    backup = BackupService(s.services.root)
    snapshot = backup.snapshot()
    run(s)
    backup.stage(snapshot)
    with InstanceLock(s.services.root) as lock:
        backup.apply_pending(lock)
    restored = build_services(s.services.root)
    with restored.db.connect() as db:
        data = db.execute("SELECT * FROM media_attempts WHERE capability=?", (s.kind,)).fetchall()
        assert len(data) == 2 and data[0]["request_id"] == data[1]["request_id"]
        assert db.execute("SELECT COUNT(*) FROM " + s.kind + "_usage").fetchone()[0] == 2
    assert (
        restored.budget.summary(restored.config.current)[s.kind]["recent"][0]["route_id"]
        == "fallback_1"
    )


def test_asr_cloud_fallback_requires_explicit_upload_permission(tmp_path):
    with pytest.raises(ValidationError, match="上传录音"):
        ASRSettings.model_validate(
            {"fallbacks": {"fallback_1": {"enabled": True, "provider": "openai_compatible"}}}
        )
    services = build_services(tmp_path)
    services.config.current.asr.enabled = True
    services.recognition.local.ready = lambda: True
    cfg = services.config.current.asr
    cfg.fallbacks["fallback_1"] = ASREndpoint(
        enabled=True, provider="openai_compatible", model="fixture-model"
    )
    services.secrets.save({"asr_fallback_1": "must-not-send-fixture"})
    calls = []

    async def transcribe(*args):
        calls.append(True)
        raise AssertionError("Should not upload")

    services.providers.resolve("asr", "fallback_1").transcribe = transcribe
    with pytest.raises(ASRError, match="未授权"):
        asyncio.run(services.recognition.transcribe(wav()))
    assert not calls


@pytest.mark.parametrize("kind", ["tts", "asr"])
def test_duplicate_enabled_routes_and_unregistered_provider_are_rejected(kind):
    cls = SpeechSettings if kind == "tts" else ASRSettings
    payload = {
        "fallbacks": {
            "fallback_1": {"enabled": True, "provider": "edge" if kind == "tts" else "local"}
        }
    }
    with pytest.raises(ValidationError):
        cls.model_validate(payload)
    with pytest.raises(ValidationError):
        cls.model_validate({"fallbacks": {"custom": {}}})
    with pytest.raises(ValidationError):
        cls.model_validate({"fallbacks": {"fallback_1": {"provider": "image"}}})


def test_schema_twenty_two_upgrade_preserves_existing_usage(tmp_path):
    services = build_services(tmp_path)
    with services.db.connect() as db:
        db.execute("DROP TABLE media_attempts")
        db.execute(
            "INSERT INTO tts_usage VALUES ('old','2026-11-08','edge','voice','explicit','completed',3,0,123,NULL)"
        )
        db.execute("UPDATE metadata SET value='22' WHERE key='schema_version'")
    db = Database(services.db.path)
    Database(services.db.path)
    assert db.get("schema_version") == "24"
    assert (
        services.budget.summary(services.config.current)["tts"]["recent"][0]["route_id"]
        == "primary"
    )


def test_three_attempts_use_distinct_keys_and_share_one_request_id(s):
    second = s.cfg.fallbacks["fallback_1"].model_copy(deep=True)
    if s.kind == "tts":
        second.compatible.base_url = "https://second-backup.example/v1"
    else:
        second.base_url = "https://second-backup.example/v1"
    s.cfg.fallbacks["fallback_2"] = second
    s.services.secrets.save({s.kind + "_fallback_2": "second-backup-fixture-key"})
    s.services.providers.resolve(s.kind, "fallback_1").transport = httpx.MockTransport(
        lambda request: httpx.Response(503)
    )

    def handler(request):
        assert request.headers["authorization"] == "Bearer second-backup-fixture-key"
        assert request.url.host == "second-backup.example"
        return (
            httpx.Response(200, content=wav())
            if s.kind == "tts"
            else httpx.Response(200, json={"text": "本地第三次尝试夹具"})
        )

    s.services.providers.resolve(s.kind, "fallback_2").transport = httpx.MockTransport(handler)
    assert run(s).provider_id == "fallback_2"
    data = rows(s)
    assert len(data) == 3 and len({r["request_id"] for r in data}) == 1
    assert [r["route_id"] for r in data] == ["primary", "fallback_1", "fallback_2"]
    assert data[1]["status"] == "uncertain" and data[1]["cost_cny"] > 0


def test_failed_cloud_attempt_keeps_own_price_and_never_reuses_backup_key(s):
    s.cfg.provider = "openai_compatible"
    if s.kind == "tts":
        s.cfg.compatible.model = "primary-fixture-model"
        s.cfg.compatible.voice = "primary-fixture-voice"
        s.cfg.compatible.price_cny_per_1000_chars = 10
    else:
        s.cfg.model = "primary-fixture-model"
        s.cfg.price_cny_per_minute = 10

    def primary(request):
        assert request.headers["authorization"] == "Bearer primary-fixture-key"
        return httpx.Response(503, text="private upstream error")

    s.services.providers.resolve(s.kind, "openai_compatible").transport = httpx.MockTransport(
        primary
    )
    assert run(s).provider_id == "fallback_1"
    data = rows(s)
    assert len(data) == 2 and data[0]["cost_cny"] > 0 and data[1]["cost_cny"] > 0
    assert data[0]["status"] == "uncertain"


@pytest.mark.parametrize("permission", ["deny", "approval"])
def test_revoking_tts_tool_permission_before_backup_blocks_external_generation(
    tmp_path, permission
):
    services = build_services(tmp_path)
    cfg = services.config.current.tts
    cfg.enabled = True
    cfg.fallbacks["fallback_1"] = SpeechEndpoint(enabled=True, edge={"voice": "zh-CN-YunxiNeural"})
    calls = []

    async def primary(*args):
        calls.append(True)
        services.config.current.tools.synthesize_speech = permission
        raise SpeechError("network", "local fixture", True)

    services.providers.resolve("tts", "edge").synthesize = primary
    with pytest.raises(SpeechError, match="权限已关闭"):
        asyncio.run(services.speech.synthesize("公开测试文字", "proactive_draft"))
    assert len(calls) == 1


def test_saved_disabled_edge_backup_can_list_real_provider_voices_without_synthesis(tmp_path):
    services = build_services(tmp_path)
    services.config.current.tts.fallbacks["fallback_1"] = SpeechEndpoint(
        enabled=False, edge={"voice": "zh-CN-YunxiNeural"}
    )

    async def voices():
        return [
            {"id": "zh-CN-YunxiNeural", "name": "Explicit local voice fixture", "locale": "zh-CN"}
        ]

    services.providers.resolve("tts", "edge").voices = voices
    with TestClient(create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}) as c:
        c.post(
            "/api/auth/setup",
            json={"username": "tester", "password": "voice-route-fixture-password"},
        ).raise_for_status()
        response = c.get("/api/tts/voices?provider_id=fallback_1")
        assert (
            response.status_code == 200 and response.json()["items"][0]["id"] == "zh-CN-YunxiNeural"
        )
        assert c.get("/api/tts/voices?provider_id=fallback_2").status_code == 422
        assert services.budget.summary(services.config.current)["tts"]["calls"] == 0
