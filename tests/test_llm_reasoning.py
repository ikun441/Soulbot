import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.providers.llm.base import ProviderError
from soulbot.providers.llm.openai_compatible import OpenAICompatibleProvider
from soulbot.services.config_service import LLMEndpoint
from soulbot.services.vision_probe import verify_vision


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    cfg = s.config.current
    cfg.llm.model = "fixture-reasoning"
    cfg.llm.retries = 0
    cfg.llm.input_price_cny_per_million = 1
    cfg.llm.output_price_cny_per_million = 2
    s.secrets.save({"llm_api_key": "reasoning-fixture-key"})
    return s


def response(reason="length", text="", calls=None, usage=True):
    value = {
        "choices": [
            {
                "message": {
                    "content": text,
                    "reasoning_content": "must never leak",
                    "tool_calls": calls,
                },
                "finish_reason": reason,
            }
        ]
    }
    if usage:
        value["usage"] = {
            "prompt_tokens": 255,
            "completion_tokens": 128,
            "completion_tokens_details": {"reasoning_tokens": 128},
        }
    return value


@pytest.mark.parametrize("effort", ["provider", "none", "low", "high", "max"])
def test_reasoning_parameter_is_explicit_and_defaults_to_absent(effort):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=response("stop", "红色"))

    p = OpenAICompatibleProvider(
        LLMEndpoint(model="fixture", reasoning_effort=effort),
        "fixture",
        httpx.MockTransport(handle),
    )
    result = asyncio.run(p.complete([{"role": "user", "content": "颜色"}], 512))
    assert result.text == "红色" and "must never leak" not in repr(result)
    if effort == "provider":
        assert "reasoning_effort" not in sent[0]
    else:
        assert sent[0]["reasoning_effort"] == effort
    with pytest.raises(ValidationError):
        LLMEndpoint(reasoning_effort="arbitrary-provider-json")


@pytest.mark.parametrize(
    "reason,kind",
    [
        ("length", "output_limit"),
        ("content_filter", "content_filter"),
        ("aborted", "generation_interrupted"),
        ("insufficient_system_resource", "generation_interrupted"),
    ],
)
def test_incomplete_generation_is_classified_and_measured(s, reason, kind):
    s.llm.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=response(reason, "不完整文本"))
    )
    state = {}
    with pytest.raises(ProviderError) as error:
        asyncio.run(
            s.llm.complete(
                [{"role": "user", "content": "你好"}],
                tools=[],
                include_memory=False,
                task_state=state,
            )
        )
    assert error.value.kind == kind and error.value.uncertain
    assert "must never leak" not in str(error.value)
    with s.db.connect() as db:
        rows = db.execute("SELECT * FROM llm_usage").fetchall()
    assert len(rows) == 1  # never silently retry the same incomplete generation
    row = rows[0]
    assert row["status"] == "failed" and row["error_kind"] == kind
    assert not row["estimated"] and row["input_tokens"] == 255
    assert row["output_tokens"] == row["reasoning_tokens"] == 128
    assert row["cost_cny"] == pytest.approx((255 + 128 * 2) / 1_000_000)
    assert state["tokens"] == 383


def test_truncation_without_usage_retains_uncertain_reservation(s):
    s.llm.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=response(usage=False))
    )
    with pytest.raises(ProviderError, match="推理"):
        asyncio.run(
            s.llm.complete([{"role": "user", "content": "你好"}], tools=[], include_memory=False)
        )
    with s.db.connect() as db:
        row = db.execute("SELECT * FROM llm_usage").fetchone()
    assert row["status"] == "uncertain" and row["estimated"]
    assert row["output_tokens"] > 0 and row["error_kind"] == "output_limit"


def test_truncated_tool_arguments_never_reach_agent_execution(s, monkeypatch):
    from soulbot.core.events import ChatEvent

    cfg = s.config.current
    cfg.qq.enabled = True
    cfg.llm.supports_tools = True
    calls = [
        {
            "id": "unfinished-call",
            "type": "function",
            "function": {"name": "save_memory", "arguments": '{"content":"incomplete'},
        }
    ]
    s.llm.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=response(calls=calls))
    )
    executed, sent = [], []

    async def execute(*args, **kwargs):
        executed.append(args)

    async def sender(*args, **kwargs):
        sent.append(args)

    monkeypatch.setattr(s.tools, "execute", execute)
    s.agent.executor.sender = sender
    asyncio.run(
        s.agent.process(
            [
                (
                    ChatEvent("34567", "23456", "truncated-tool", "记住我喜欢猫", directed=True),
                    "direct",
                )
            ]
        )
    )
    assert not executed and not sent
    assert not s.memory.listing()["items"]
    with s.db.connect() as db:
        assert db.execute("SELECT error_kind FROM llm_usage").fetchone()[0] == "output_limit"


@pytest.mark.parametrize(
    "done,reason,kind",
    [
        (True, "length", "output_limit"),
        (True, "content_filter", "content_filter"),
        (False, "stop", "stream_interrupted"),
    ],
)
def test_stream_finish_state_preserves_usage_and_rejects_partial_tools(s, done, reason, kind):
    s.config.current.llm.streaming = True
    s.config.current.llm.supports_tools = True
    parts = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "private reasoning"}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call",
                                "function": {"name": "save_memory", "arguments": "{"},
                            }
                        ]
                    },
                    "finish_reason": reason,
                }
            ]
        },
        {"choices": [], "usage": response()["usage"]},
    ]
    body = "".join("data: " + json.dumps(part) + "\n\n" for part in parts) + (
        "data: [DONE]\n\n" if done else ""
    )
    s.llm.transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    with pytest.raises(ProviderError) as error:
        asyncio.run(
            s.llm.complete([{"role": "user", "content": "测试"}], tools=[], include_memory=False)
        )
    assert error.value.kind == kind
    with s.db.connect() as db:
        row = db.execute("SELECT * FROM llm_usage").fetchone()
    assert row["status"] == "failed" and row["output_tokens"] == 128
    assert row["reasoning_tokens"] == 128 and not row["estimated"]


def test_successful_stream_reports_finish_reason_and_keeps_reasoning_private(s):
    s.config.current.llm.streaming = True
    body = 'data: {"choices":[{"index":0,"delta":{"reasoning_content":"secret"}}]}\n\ndata: {"choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    s.llm.transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    deltas = []

    async def delta(text):
        deltas.append(text)

    result = asyncio.run(
        s.llm.complete(
            [{"role": "user", "content": "你好"}], tools=[], include_memory=False, on_delta=delta
        )
    )
    assert result.text == "你好" and result.finish_reason == "stop"
    assert deltas == ["你好"] and "secret" not in repr(result)


def test_vision_probe_uses_configured_output_cap_and_never_enables_truncated_result(s):
    captured = []
    s.config.current.llm.supports_vision = False
    s.config.current.limits.max_output_per_request = 400

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    s.llm.transport = httpx.MockTransport(handle)
    with pytest.raises(ProviderError) as error:
        asyncio.run(verify_vision(s.llm))
    assert error.value.kind == "output_limit" and not s.config.current.llm.supports_vision
    assert captured[0]["max_tokens"] == 400
    assert s.config.current.limits.max_output_per_request == 400


def test_reasoning_config_api_round_trip_and_actionable_truncation_error(s):
    s.llm.transport = httpx.MockTransport(lambda req: httpx.Response(200, json=response()))
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as c:
        c.post(
            "/api/auth/setup",
            json={"username": "reasoning_admin", "password": "reasoning-fixture-password"},
        )
        cfg = c.get("/api/config").json()["config"]
        cfg["llm"]["reasoning_effort"] = "none"
        cfg["llm"]["fallbacks"] = {
            "fallback_1": LLMEndpoint(enabled=False, reasoning_effort="low").model_dump()
        }
        assert c.put("/api/config", json=cfg).status_code == 200
        read = c.get("/api/config").json()["config"]
        assert read["llm"]["reasoning_effort"] == "none"
        assert read["llm"]["fallbacks"]["fallback_1"]["reasoning_effort"] == "low"
        r = c.post("/api/llm/test")
        assert r.status_code == 502 and r.json()["kind"] == "output_limit"
        assert "推理" in r.json()["detail"] and "must never leak" not in r.text
        assert c.get("/api/usage").json()["recent"][0]["status"] == "failed"
