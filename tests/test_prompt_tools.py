"""Non-native tool protocol, real registry execution, and wire/accounting regressions."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.providers.llm.base import ProviderError, Usage, estimate_tokens
from soulbot.providers.llm.openai_compatible import OpenAICompatibleProvider
from soulbot.providers.llm.prompt_tools import fit_messages, prepare_messages
from soulbot.services.config_service import LLMEndpoint, Settings
from soulbot.tools.registry import Tool

SPEC = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Read-only lookup",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string", "maxLength": 20}},
            "required": ["key"],
            "additionalProperties": False,
        },
    },
}
CALL = {"type": "tool_calls", "calls": [{"name": "lookup", "arguments": {"key": "猫"}}]}
MESSAGES = [{"role": "system", "content": "角色设定"}, {"role": "user", "content": "lookup"}]


def response(content, **kwargs):
    message = {"content": content, **kwargs.pop("message", {})}
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message, "finish_reason": kwargs.pop("finish", "stop")}],
            "usage": {"prompt_tokens": 31, "completion_tokens": 12},
            **kwargs,
        },
    )


def provider(content, *, streaming=False, **kwargs):
    settings = LLMEndpoint(
        model="fixture", supports_tools=True, tool_calling_mode="json", streaming=streaming
    )
    if streaming:
        chunks = [content[:10], content[10:]]
        body = "".join(
            "data: " + json.dumps({"choices": [{"delta": {"content": part}}]}) + "\n\n"
            for part in chunks
        )
        body += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":31,"completion_tokens":12}}\n\n'
        body += "data: [DONE]\n\n"
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    else:
        transport = httpx.MockTransport(lambda _: response(content, **kwargs))
    return OpenAICompatibleProvider(settings, "fixture-secret", transport)


def run(coro):
    return asyncio.run(coro)


def test_json_wire_has_no_native_tools_and_accepts_only_declared_schema():
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        assert not {"tools", "tool_choice", "response_format"} & set(payload)
        assert all(m["role"] != "tool" and "tool_calls" not in m for m in payload["messages"])
        assert payload["messages"][0] == MESSAGES[0]
        assert '"name":"lookup"' in payload["messages"][1]["content"]
        seen.append(payload)
        return response(json.dumps(CALL))

    p = provider("")
    p.transport = httpx.MockTransport(handler)
    result = run(p.complete(MESSAGES, 512, [SPEC]))
    assert result.text == "" and result.usage == Usage(31, 12)
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"key": "猫"}
    assert result.tool_calls[0]["id"].startswith("json_")
    assert len(seen) == 1 and MESSAGES[0]["content"] == "角色设定"


@pytest.mark.parametrize(
    "content",
    [
        "plain text",
        "```json\n{}\n```",
        'prefix {"type":"final","text":"x"}',
        '{"type":"final","text":"x"} trailing',
        "[]",
        "null",
        '{"type":"final","text":1}',
        '{"type":"final","text":""}',
        '{"type":"final","text":"x","approval":true}',
        '{"type":"final","text":"x","text":"y"}',
        '{"type":"tool_calls","calls":[]}',
        '{"type":"tool_calls","calls":[{"name":"shell","arguments":{}}]}',
        '{"type":"tool_calls","calls":[{"name":"lookup","arguments":{"key":1}}]}',
        '{"type":"tool_calls","calls":[{"name":"lookup","arguments":{"key":"x","approved":true}}]}',
        '{"type":"tool_calls","calls":[{"name":"lookup","arguments":"{}"}]}',
        '{"type":"tool_calls","calls":[{"name":"lookup","arguments":{"key":NaN}}]}',
        '{"type":"tool_calls","calls":[{"name":"lookup","arguments":{"key":"x","key":"y"}}]}',
        json.dumps({"type": "tool_calls", "calls": CALL["calls"] * 2}),
        json.dumps(
            {
                "type": "tool_calls",
                "calls": [{"name": "lookup", "arguments": {"key": str(i)}} for i in range(6)],
            }
        ),
        json.dumps(
            {"type": "tool_calls", "calls": [*CALL["calls"], {"name": "shell", "arguments": {}}]}
        ),
        '{"type":"final","text":' + "[" * 1200 + "0" + "]" * 1200 + "}",
    ],
)
def test_invalid_whole_proposal_is_rejected_with_actual_usage(content):
    with pytest.raises(ProviderError) as caught:
        run(provider(content).complete(MESSAGES, 512, [SPEC]))
    assert caught.value.kind == "invalid_tool_call"
    assert caught.value.usage == Usage(31, 12)
    assert content not in str(caught.value)


@pytest.mark.parametrize("streaming", [False, True])
def test_compatible_stream_never_leaks_partial_json_or_calls(streaming):
    seen = []

    async def delta(text):
        seen.append(text)

    p = provider(json.dumps(CALL), streaming=streaming)
    result = run(p.complete(MESSAGES, 512, [SPEC], delta))
    assert result.tool_calls and not seen
    p = provider('{"type":"final","text":"你好"}', streaming=streaming)
    assert run(p.complete(MESSAGES, 512, [SPEC], delta)).text == "你好"
    assert seen == ["你好"]
    p = provider('{"type":"tool_calls","calls":[', streaming=streaming)
    with pytest.raises(ProviderError):
        run(p.complete(MESSAGES, 512, [SPEC], delta))
    assert seen == ["你好"]


def test_length_limit_blocks_proposal_and_native_calls_are_rejected_in_compatibility_mode():
    with pytest.raises(ProviderError) as caught:
        run(provider(json.dumps(CALL), finish="length").complete(MESSAGES, 10, [SPEC]))
    assert caught.value.kind == "output_limit"
    with pytest.raises(ProviderError) as caught:
        run(
            provider(
                '{"type":"final","text":"x"}',
                message={
                    "tool_calls": [
                        {
                            "id": "external",
                            "function": {"name": "lookup", "arguments": '{"key":"x"}'},
                        }
                    ]
                },
            ).complete(MESSAGES, 512, [SPEC])
        )
    assert caught.value.kind == "invalid_tool_call"


def test_plain_chat_json_is_not_interpreted_and_disabled_capability_still_blocks_tools():
    raw = json.dumps(CALL)
    p = provider(raw)
    assert run(p.complete(MESSAGES, 512)).text == raw
    p.settings.supports_tools = False
    with pytest.raises(ProviderError) as caught:
        run(p.complete(MESSAGES, 512, [SPEC]))
    assert caught.value.kind == "unsupported_capability"
    assert Settings().llm.tool_calling_mode == "native"
    with pytest.raises(ValidationError):
        LLMEndpoint(tool_calling_mode="auto")


def test_history_translation_preserves_tool_data_and_native_history_is_unchanged():
    result = run(provider(json.dumps(CALL)).complete(MESSAGES, 512, [SPEC]))
    messages = [
        *MESSAGES,
        {"role": "assistant", "content": None, "tool_calls": result.tool_calls},
        {
            "role": "tool",
            "tool_call_id": result.tool_calls[0]["id"],
            "content": '{"error":"denied"}',
        },
    ]
    wire = prepare_messages(messages, [SPEC], "json")
    assert json.loads(wire[-2]["content"]) == CALL
    data = json.loads(wire[-1]["content"])
    assert data["name"] == "lookup" and json.loads(data["content"])["error"] == "denied"
    assert data["arguments"] == {"key": "猫"}
    assert wire[-1]["role"] == "user"
    assert prepare_messages(messages, [SPEC], "native") == messages
    with pytest.raises(ProviderError) as caught:
        prepare_messages(
            [{"role": "tool", "tool_call_id": "orphan", "content": "x"}], [SPEC], "json"
        )
    assert caught.value.kind == "invalid_tool_history"


def test_protocol_tokens_are_reserved_and_canonical_old_turns_can_be_trimmed():
    endpoint = LLMEndpoint(tool_calling_mode="json", context_window=6000)
    messages = [
        MESSAGES[0],
        {"role": "user", "content": "old" * 1500},
        {"role": "assistant", "content": "old answer"},
        MESSAGES[1],
    ]
    bounded, amount = fit_messages(messages, [SPEC], endpoint, 512)
    assert bounded == MESSAGES
    assert amount == estimate_tokens(prepare_messages(bounded, [SPEC], "json"))
    assert amount > estimate_tokens(bounded)
    with pytest.raises(ProviderError):
        fit_messages(
            MESSAGES, [SPEC], LLMEndpoint(context_window=1024, tool_calling_mode="json"), 512
        )


@pytest.fixture
def services(tmp_path):
    s = build_services(tmp_path)
    cfg = s.config.current
    cfg.qq.enabled = True
    cfg.personality.owner_qq = "23456"
    cfg.llm.model = "fixture"
    cfg.llm.supports_tools = True
    cfg.llm.tool_calling_mode = "json"
    cfg.llm.retries = 0
    cfg.memory.global_context = False
    cfg.diary.auto_generate = False
    s.secrets.save({"llm_api_key": "fixture-secret"})
    return s


class Lookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=20)


@pytest.mark.parametrize("policy", ["auto", "approval", "deny"])
def test_actual_agent_loop_uses_registry_and_permissions(services, monkeypatch, policy):
    s = services
    cfg = s.config.current
    cfg.tools.search_web = policy
    executed, bodies = [], []

    async def handle(args):
        executed.append(args.key)
        return {"answer": "read-only fact"}

    # Existing configurable permission name, isolated read-only handler.
    s.tools.tools["search_web"] = Tool("search_web", "read only", Lookup, handle)
    s.tools.argument_guard = s.tools.before_execute = None
    spec = {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "read-only",
            "parameters": Lookup.model_json_schema(),
        },
    }
    monkeypatch.setattr("soulbot.tools.media_tools.specifications", lambda *_: [spec])

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return response(
                json.dumps(
                    {
                        "type": "tool_calls",
                        "calls": [{"name": "search_web", "arguments": {"key": "fact"}}],
                    }
                )
            )
        result = json.loads(body["messages"][-1]["content"])
        assert result["type"] == "tool_result"
        assert ("answer" in json.loads(result["content"])) == (policy == "auto")
        return response('{"type":"final","text":"done"}')

    s.llm.transport = httpx.MockTransport(handler)
    event = ChatEvent("34567", "23456", "fixture", "lookup", directed=True)
    result, attachments = run(s.agent.answer(MESSAGES, event, "explicit"))
    assert result.text == "done" and attachments == []
    assert executed == (["fact"] if policy == "auto" else [])
    assert len(bodies) == 2
    queued = s.approvals.listing()["items"]
    assert len(queued) == (1 if policy == "approval" else 0)
    if queued:
        assert queued[0]["status"] == "pending"
    assert s.budget.summary(cfg)["recent"][0]["input_tokens"] == 31


@pytest.mark.parametrize("fail_followup", [False, True])
def test_read_only_capability_probe_checks_result_before_enable(services, fail_followup):
    s = services
    s.config.current.llm.supports_tools = False
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return response(
                '{"type":"tool_calls","calls":[{"name":"capability_probe","arguments":{"word":"ok"}}]}'
            )
        value = json.loads(json.loads(body["messages"][-1]["content"])["content"])["value"]
        return response(json.dumps({"type": "final", "text": "wrong" if fail_followup else value}))

    s.llm.transport = httpx.MockTransport(handler)
    if fail_followup:
        with pytest.raises(ProviderError):
            run(s.llm.verify_tools())
        assert not s.config.current.llm.supports_tools
    else:
        result = run(s.llm.verify_tools())
        assert result["mode"] == "json" and s.config.current.llm.supports_tools
    assert len(bodies) == 2


def test_capability_probe_config_change_and_authenticated_api(services):
    s = services
    s.config.current.llm.supports_tools = False
    app = create_web_app(s.root, s)
    c = TestClient(app, headers={"X-Soulbot-Request": "1"})
    assert c.post("/api/llm/tools-test").status_code == 401
    c.headers["X-Soulbot-Request"] = "1"
    assert (
        c.post(
            "/api/auth/setup", json={"username": "tester", "password": "test-password-123"}
        ).status_code
        == 200
    )

    def handler(request):
        body = json.loads(request.content)
        if (
            body["messages"][-1]["role"] == "user"
            and "tool_result" in body["messages"][-1]["content"]
        ):
            value = json.loads(json.loads(body["messages"][-1]["content"])["content"])["value"]
            s.config.current.llm.tool_calling_mode = "native"
            return response(json.dumps({"type": "final", "text": value}))
        return response(
            '{"type":"tool_calls","calls":[{"name":"capability_probe","arguments":{"word":"ok"}}]}'
        )

    s.llm.transport = httpx.MockTransport(handler)
    result = c.post("/api/llm/tools-test")
    assert result.status_code >= 400 and not s.config.current.llm.supports_tools


def test_fallback_changes_protocol_only_when_explicitly_configured(services):
    s = services
    s.config.current.llm.tool_calling_mode = "native"
    s.config.current.llm.fallbacks["fallback_1"] = LLMEndpoint(
        base_url="https://backup.invalid/v1",
        model="fallback-fixture",
        supports_tools=True,
        tool_calling_mode="json",
        retries=0,
    )
    s.secrets.save({"llm_fallback_1": "independent-fixture-secret"})
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if request.url.host != "backup.invalid":
            assert payload["tools"] == [SPEC]
            return httpx.Response(400)
        assert "tools" not in payload and "tool_choice" not in payload
        assert request.headers["authorization"] == "Bearer independent-fixture-secret"
        return response(json.dumps(CALL))

    s.llm.transport = httpx.MockTransport(handler)
    result = run(s.llm.complete(MESSAGES, tools=[SPEC]))
    assert result.tool_calls and result.provider == "fallback_1" and len(seen) == 2
    recent = s.budget.summary(s.config.current)["recent"]
    assert len(recent) == 2 and recent[0]["input_tokens"] == 31


def test_cancelled_stream_does_not_return_or_execute_partial_proposal(services):
    s = services
    s.config.current.llm.streaming = True
    started = asyncio.Event()

    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"{\\"type\\":\\"tool_calls\\","}}]}\n\n'
            started.set()
            await asyncio.Event().wait()

    s.llm.transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=WaitingBody()))
    seen = []

    async def delta(text):
        seen.append(text)

    async def check():
        task = asyncio.create_task(s.llm.complete(MESSAGES, tools=[SPEC], on_delta=delta))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(check())
    assert not seen
    with s.db.connect() as db:
        assert db.execute("SELECT status FROM llm_usage").fetchone()[0] == "uncertain"


def test_first_probe_config_change_prevents_second_request(services):
    s = services
    seen = []
    s.config.current.llm.supports_tools = False

    def handler(request):
        seen.append(request)
        s.config.current.llm.model = "changed-model"
        return response(
            '{"type":"tool_calls","calls":[{"name":"capability_probe","arguments":{"word":"ok"}}]}'
        )

    s.llm.transport = httpx.MockTransport(handler)
    with pytest.raises(ProviderError) as caught:
        run(s.llm.verify_tools())
    assert caught.value.kind == "config_changed"
    assert len(seen) == 1 and not s.config.current.llm.supports_tools


def test_parallel_same_tool_results_remain_bound_to_arguments():
    calls = [
        {"id": f"call-{key}", "function": {"name": "lookup", "arguments": json.dumps({"key": key})}}
        for key in ["one", "two"]
    ]
    messages = [
        *MESSAGES,
        {"role": "assistant", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "call-two", "content": "second result"},
        {"role": "tool", "tool_call_id": "call-one", "content": "first result"},
    ]
    wire = prepare_messages(messages, [SPEC], "json")
    assert json.loads(wire[-2]["content"])["arguments"] == {"key": "two"}
    assert json.loads(wire[-1]["content"])["arguments"] == {"key": "one"}


def test_json_discovery_loads_only_requested_tools_then_executes(services, monkeypatch):
    s = services
    s.config.current.tools.search_web = "auto"
    s.config.current.llm.context_window = 14000
    s.tools.argument_guard = s.tools.before_execute = None
    executed, bodies = [], []

    async def lookup(args):
        executed.append(args.key)
        return {"answer": "fact"}

    s.tools.tools["search_web"] = Tool("search_web", "lookup", Lookup, lookup)
    discovery = s.tools.tools["discover_tools"]
    specs = [
        {
            "type": "function",
            "function": {
                "name": discovery.name,
                "description": discovery.description,
                "parameters": discovery.schema.model_json_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "lookup",
                "parameters": Lookup.model_json_schema(),
            },
        },
        *[
            {
                "type": "function",
                "function": {
                    "name": f"large_{i}",
                    "description": "x" * 1500,
                    "parameters": Lookup.model_json_schema(),
                },
            }
            for i in range(12)
        ],
    ]
    monkeypatch.setattr("soulbot.tools.media_tools.specifications", lambda *_: specs)

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        instruction = next(
            m["content"]
            for m in body["messages"]
            if m["role"] == "system" and "SoulBot JSON工具协议" in m["content"]
        )
        available = json.loads(instruction.split("可用工具及参数Schema：\n")[1])
        names = {spec["function"]["name"] for spec in available}
        if len(bodies) == 1:
            assert names == {"discover_tools"}
            return response(
                '{"type":"tool_calls","calls":[{"name":"discover_tools","arguments":{"names":["search_web"]}}]}'
            )
        assert "search_web" in names and not any(name.startswith("large_") for name in names)
        if len(bodies) == 2:
            return response(
                '{"type":"tool_calls","calls":[{"name":"search_web","arguments":{"key":"fact"}}]}'
            )
        return response('{"type":"final","text":"done"}')

    s.llm.transport = httpx.MockTransport(handler)
    event = ChatEvent("34567", "23456", "discovery-fixture", "lookup", directed=True)
    result, attachments = run(s.agent.answer(MESSAGES, event, "explicit"))
    assert result.text == "done" and not attachments
    assert len(bodies) == 3 and executed == ["fact"]
