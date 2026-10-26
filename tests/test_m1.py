import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.core.agent import SoulBotAgent
from soulbot.core.budget import BudgetService
from soulbot.core.conversation import ConversationStore
from soulbot.core.decision import DecisionEngine
from soulbot.core.events import ChatEvent
from soulbot.providers.llm.base import Completion, ProviderError, Usage
from soulbot.providers.llm.openai_compatible import OpenAICompatibleProvider
from soulbot.providers.registry import ProviderRegistry
from soulbot.services.config_service import ConfigService, LLMEndpoint, Settings
from soulbot.services.llm_service import LLMService
from soulbot.services.secret_service import SecretService
from soulbot.storage.database import Database


def run(coro):
    return asyncio.run(coro)


def event(text="hello", *, group="12345", user="23456", **kwargs):
    return ChatEvent("34567", user, str(time.time_ns()), text, group_id=group, **kwargs)


def test_openai_compatible_wire_model_list_and_completion():
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer secret-fixture"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 41
        assert payload["stream"] is False
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
            },
        )

    provider = OpenAICompatibleProvider(
        LLMEndpoint(
            base_url="https://fixture.invalid/v1",
            model="fixture-model",
            max_output_field="max_completion_tokens",
        ),
        "secret-fixture",
        httpx.MockTransport(respond),
    )

    async def check():
        assert await provider.models() == ["fixture-model"]
        result = await provider.complete([{"role": "user", "content": "嗨"}], 41)
        assert (result.text, result.usage.input_tokens, result.usage.reasoning_tokens) == (
            "你好",
            5,
            1,
        )

    run(check())
    assert len(requests) == 2
    assert "secret-fixture" not in repr(requests[-1].headers)


@pytest.mark.parametrize(
    "status,kind,retryable",
    [
        (401, "authentication", False),
        (404, "model_not_found", False),
        (429, "rate_limit", True),
        (503, "unavailable", True),
    ],
)
def test_provider_classifies_http_errors_without_echoing_body(status, kind, retryable):
    provider = OpenAICompatibleProvider(
        LLMEndpoint(base_url="https://fixture.invalid/v1", model="fixture"),
        "fixture-secret",
        httpx.MockTransport(lambda request: httpx.Response(status, text="private-provider-body")),
    )

    async def check():
        with pytest.raises(ProviderError) as caught:
            await provider.complete([{"role": "user", "content": "x"}], 16)
        assert caught.value.kind == kind
        assert caught.value.retryable is retryable
        assert "private-provider-body" not in str(caught.value)
        assert "fixture-secret" not in str(caught.value)

    run(check())


def test_streaming_sse_assembles_content_and_usage():
    lines = [
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"呀"}}]}\n\n',
        'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    ]
    provider = OpenAICompatibleProvider(
        LLMEndpoint(base_url="https://fixture.invalid/v1", model="fixture", streaming=True),
        "secret",
        httpx.MockTransport(lambda request: httpx.Response(200, content="".join(lines))),
    )
    seen = []

    async def check():
        result = await provider.complete(
            [{"role": "user", "content": "x"}], 16, on_delta=lambda text: append(seen, text)
        )
        assert result.text == "好呀"
        assert result.usage == Usage(7, 2)

    async def append(target, value):
        target.append(value)

    run(check())
    assert seen == ["好", "呀"]


def test_incomplete_stream_is_marked_uncertain():
    provider = OpenAICompatibleProvider(
        LLMEndpoint(base_url="https://fixture.invalid/v1", model="fixture", streaming=True),
        "secret",
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, content='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            )
        ),
    )

    async def check():
        with pytest.raises(ProviderError) as caught:
            await provider.complete([{"role": "user", "content": "x"}], 16)
        assert caught.value.kind == "stream_interrupted"
        assert caught.value.uncertain

    run(check())


def test_budget_unknown_cost_is_reserved_and_persisted(tmp_path):
    db = Database(tmp_path / "data" / "soulbot.db")
    config = Settings()
    budget = BudgetService(db)
    key = budget.reserve(config, config.llm, "primary", "explicit", 100, 50)
    budget.settle(key, config.llm, None)
    result = budget.summary(config)
    assert result["reserved_or_used_tokens"] == 150
    assert result["unknown_cost_calls"] == 1
    assert result["known_or_estimated_cost_cny"] == 0
    with pytest.raises(ProviderError, match="未知费用"):
        budget.reserve(config, config.llm, "primary", "proactive", 100, 50)
    restarted = BudgetService(Database(tmp_path / "data" / "soulbot.db"))
    assert restarted.summary(config)["unknown_cost_calls"] == 1


def test_concurrent_budget_reservations_cannot_overspend_tokens(tmp_path):
    db = Database(tmp_path / "data" / "soulbot.db")
    config = Settings()
    config.behavior.daily_tokens = 1000
    budget = BudgetService(db)

    def reserve(index):
        try:
            return budget.reserve(config, config.llm, "primary", f"task-{index}", 500, 200)
        except ProviderError as error:
            return error.kind

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(reserve, (1, 2)))
    assert sum(value != "budget" for value in result) == 1
    assert result.count("budget") == 1
    assert budget.summary(config)["reserved_or_used_tokens"] == 700


def test_llm_service_falls_back_only_to_configured_provider(tmp_path):
    config = ConfigService(tmp_path)
    settings = config.current.model_copy(deep=True)
    settings.llm.base_url = "https://primary.invalid/v1"
    settings.llm.model = "primary-model"
    settings.llm.retries = 0
    settings.llm.fallbacks = {
        "fallback_1": LLMEndpoint(
            base_url="https://backup.invalid/v1", model="backup-model", retries=0
        )
    }
    config.save(settings)
    secrets = SecretService(tmp_path)
    secrets.save({"llm_api_key": "primary-key", "llm_fallback_1": "backup-key"})
    db = Database(tmp_path / "data" / "soulbot.db")
    service = LLMService(config, secrets, ProviderRegistry(), BudgetService(db), db.audit)

    def respond(request):
        if request.url.host == "primary.invalid":
            return httpx.Response(503, text="provider internal detail")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "backup response"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 6, "completion_tokens": 2},
            },
        )

    service.transport = httpx.MockTransport(respond)

    async def check():
        response = await service.complete([{"role": "user", "content": "hello"}])
        assert response.provider == "fallback_1"
        assert response.text == "backup response"

    run(check())
    with db.connect() as connection:
        audit = " ".join(row[0] for row in connection.execute("SELECT detail FROM audit"))
    assert "provider internal detail" not in audit


def test_database_migrates_old_schema_without_losing_metadata(tmp_path):
    db = Database(tmp_path / "data" / "soulbot.db")
    db.set("wizard_step", "5")
    reopened = Database(tmp_path / "data" / "soulbot.db")
    assert reopened.get("schema_version") == "6"
    assert reopened.get("wizard_step") == "5"
    with reopened.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0


def test_group_filtering_and_persisted_mute_and_cooldown(tmp_path):
    db = Database(tmp_path / "data" / "soulbot.db")
    config = Settings.model_validate({"qq": {"enabled": True}, "behavior": {"quiet_hours": False}})
    now = [1_800_000_000.0]
    store = ConversationStore(db, clock=lambda: now[0])
    decision = DecisionEngine(db, store, clock=lambda: now[0], rng=lambda: 1.0)
    normal = event("ordinary text")
    assert decision.classify(config, normal) is None
    assert decision.classify(config, event("hello", directed=True)) == "direct"
    assert decision.classify(config, event("hello", mentions_other=True)) is None
    assert decision.classify(config, event("hello", reply_target="other")) is None
    assert decision.classify(config, event("hello", is_bot=True)) is None
    wake_config = config.model_copy(deep=True)
    wake_config.behavior.wake_words = ["桃桃"]
    assert decision.classify(wake_config, event("桃桃 你好")) == "direct"
    store.remember(normal.scope, "hi", "hello", config)
    assert decision.classify(config, event("你觉得呢")) == "followup"
    assert decision.claim(config, normal, "direct")
    assert not decision.claim(config, normal, "direct")
    decision.mute(normal, 60)
    assert decision.classify(config, event("@ bot", directed=True)) is None
    now[0] += 61
    assert decision.classify(config, event("@ bot", directed=True)) == "direct"


def test_context_deduplication_tenant_isolation_and_clear(tmp_path):
    db = Database(tmp_path / "data" / "soulbot.db")
    now = [1000.0]
    store = ConversationStore(db, clock=lambda: now[0])
    config = Settings()
    private = ChatEvent("bot", "user", "1", "secret")
    group = ChatEvent("bot", "user", "2", "public", group_id="12345")
    assert store.unseen(private)
    assert not store.unseen(private)
    store.remember(private.scope, "secret", "answer", config)
    store.remember(group.scope, "public", "group answer", config)
    assert store.history(private.scope, 8)[0]["content"] == "secret"
    assert store.history(group.scope, 8)[0]["content"] == "public"
    now[0] += 25 * 3600
    assert store.history(private.scope, 8) == []
    store.clear()
    assert store.last_reply(private.scope) == 0


def test_agent_batches_directed_messages_and_ignores_ordinary_group_chat(tmp_path):
    services = build_services(tmp_path)
    config = services.config.current.model_copy(deep=True)
    config.qq.enabled = True
    config.behavior.group_batch_seconds = 0.03
    services.secrets.save({"onebot_access_token": "fixture-onebot-token"})
    services.config.save(config)

    class FakeLLM:
        calls = []

        def configured(self):
            return True

        async def complete(self, messages, **kwargs):
            self.calls.append(messages[-1]["content"])
            return Completion("fixture answer", Usage(4, 2))

    llm = FakeLLM()
    agent = SoulBotAgent(services.config, services.db, llm)
    sent = []

    async def sender(message, text):
        sent.append((message.group_id, text))

    agent.executor.sender = sender

    async def check():
        normal = event("ordinary group message")
        await agent.accept(normal)
        assert not agent.pending
        await agent.accept(event("@soul first", directed=True))
        await agent.accept(event("and second", directed=True))
        await asyncio.sleep(0.1)
        assert llm.calls == ["@soul first\nand second"]
        assert sent == [("12345", "fixture answer")]
        await agent.close()

    run(check())


def test_llm_api_auth_and_test_chat_use_real_fixture(tmp_path):
    services = build_services(tmp_path)
    config = services.config.current.model_copy(deep=True)
    config.llm.base_url = "https://fixture.invalid/v1"
    config.llm.model = "fixture-model"
    config.llm.retries = 0
    services.config.save(config)
    services.secrets.save({"llm_api_key": "local-fixture-secret"})

    def respond(request):
        assert request.headers["authorization"] == "Bearer local-fixture-secret"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "fixture reply"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 2},
            },
        )

    services.llm.transport = httpx.MockTransport(respond)
    client = TestClient(create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"})
    assert client.post("/api/llm/test", headers={"X-Soulbot-Request": "1"}).status_code == 401
    assert (
        client.post(
            "/api/auth/setup",
            headers={"X-Soulbot-Request": "1"},
            json={"username": "fixture_admin", "password": "fixture-password-123"},
        ).status_code
        == 200
    )
    response = client.post("/api/llm/test", headers={"X-Soulbot-Request": "1"})
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "fixture reply"
    assert response.json()["usage"]["input_tokens"] == 9
    chat = client.post("/api/chat", json={"text": "say hello"})
    assert chat.status_code == 200, chat.text
    assert chat.json()["text"] == "fixture reply"
    assert "local-fixture-secret" not in client.get("/api/config").text
    assert client.get("/api/usage").json()["reserved_or_used_tokens"] == 22


def test_send_test_only_accepts_explicit_confirmation(tmp_path):
    services = build_services(tmp_path)
    config = services.config.current.model_copy(deep=True)
    config.personality.owner_qq = "123456789"
    config.qq.enabled = True
    services.secrets.save({"onebot_access_token": "fixture-onebot-token"})
    services.config.save(config)
    calls = []

    class Transport:
        def connected(self):
            return ["10001"]

    services.qq_transport = Transport()

    async def sender(event, text):
        calls.append((event.user_id, text))

    services.agent.executor.sender = sender
    client = TestClient(create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"})
    client.post(
        "/api/auth/setup", json={"username": "fixture_admin", "password": "fixture-password-123"}
    )
    assert client.post("/api/qq/send-test", json={"confirm": False}).status_code == 422
    result = client.post("/api/qq/send-test", json={"confirm": True})
    assert result.status_code == 200, result.text
    assert calls and calls[0][0] == "123456789"
    assert len(calls) == 1
