import asyncio
import io
import json
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from soulbot.app import build_services
from soulbot.core.agent import reply_parts
from soulbot.core.events import ChatEvent
from soulbot.providers.image.base import ImageGenerationError
from soulbot.providers.image.comfyui import ComfyUIProvider
from soulbot.services.config_service import ComfySettings, Settings


def png():
    out = io.BytesIO()
    Image.new("RGB", (512, 512), "pink").save(out, format="PNG")
    return out.getvalue()


def config():
    settings = Settings().image_generation
    settings.provider = "comfyui"
    settings.enabled = True
    settings.comfy.checkpoint = "fixture.safetensors"
    settings.price_cny_per_image = 0
    return settings


def mock_comfy(*, bad_path=False, failed=False):
    task = str(uuid4())
    state = {}

    def respond(request):
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "fixture"}})
        if request.url.path == "/object_info/CheckpointLoaderSimple":
            return httpx.Response(
                200,
                json={
                    "CheckpointLoaderSimple": {
                        "input": {"required": {"ckpt_name": [["fixture.safetensors"]]}}
                    }
                },
            )
        if request.url.path == "/object_info/KSampler":
            return httpx.Response(
                200,
                json={
                    "KSampler": {
                        "input": {
                            "required": {"sampler_name": [["euler"]], "scheduler": [["normal"]]}
                        }
                    }
                },
            )
        if request.url.path == "/prompt":
            body = json.loads(request.content)
            state["workflow"] = body["prompt"]
            assert state["workflow"]["6"]["inputs"]["text"] == "test prompt"
            assert state["workflow"]["5"]["inputs"]["batch_size"] == 1
            assert len(state["workflow"]) == 7
            state["prefix"] = state["workflow"]["9"]["inputs"]["filename_prefix"]
            return httpx.Response(200, json={"prompt_id": task, "node_errors": {}})
        if request.url.path == "/history/" + task:
            return httpx.Response(
                200,
                json={
                    task: {
                        "status": {
                            "status_str": "error" if failed else "success",
                            "completed": True,
                        },
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "../../secret.png"
                                        if bad_path
                                        else state["prefix"] + "_00001_.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/view":
            assert request.url.params["type"] == "output"
            return httpx.Response(200, content=png())
        raise AssertionError(str(request.url))

    return httpx.MockTransport(respond), state


def test_comfy_fixed_real_protocol_and_format():
    provider = ComfyUIProvider()
    provider.transport, state = mock_comfy()
    image = asyncio.run(provider.generate("test prompt", config()))
    assert image.mime == "image/png" and image.width == 512
    assert state["workflow"]["4"]["inputs"]["ckpt_name"] == "fixture.safetensors"
    assert asyncio.run(provider.health_check())


@pytest.mark.parametrize("bad_path,failed", [(True, False), (False, True)])
def test_comfy_no_arbitrary_file_access_or_fake_success(bad_path, failed):
    provider = ComfyUIProvider()
    provider.transport, _ = mock_comfy(bad_path=bad_path, failed=failed)
    with pytest.raises(ImageGenerationError) as error:
        asyncio.run(provider.generate("test prompt", config()))
    assert error.value.uncertain


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.2:8188", "file:///tmp/a"])
def test_comfy_only_loopback(url):
    with pytest.raises(ValidationError):
        ComfySettings(base_url=url)


def test_comfy_service_selects_capability_without_cloud_key(tmp_path):
    s = build_services(tmp_path)
    s.config.current.image_generation = config()
    provider = s.providers.resolve("image_generation", "comfyui")
    provider.transport, _ = mock_comfy()
    assert s.images.configured() and not s.secrets.get("image_api_key")
    image = asyncio.run(s.images.generate("test prompt"))
    assert image.mime == "image/png"
    assert s.images.diagnostics()["state"] == "available"
    recent = s.budget.summary(s.config.current)["image_generation"]["recent"][0]
    assert recent["provider"] == "comfyui" and recent["cost_cny"] == 0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("一句。", ["一句。"]),
        ("第一句。第二句。", ["第一句。第二句。"]),
        ("第一句。第二句！第三句？", ["第一句。第二句！", "第三句？"]),
        ("Hello. How are you? Fine!", ["Hello. How are you?", "Fine!"]),
        (
            "链接 https://example.com/a 和 1.5 模型。第二句。第三句。",
            ["链接 https://example.com/a 和 1.5 模型。第二句。", "第三句。"],
        ),
    ],
)
def test_sentence_splitting_preserves_punctuation_and_urls(text, expected):
    assert reply_parts(text, Settings()) == expected


def test_split_fanout_is_bounded_and_switchable():
    settings = Settings()
    assert len(reply_parts("喵。" * 100, settings)) == 5
    settings.behavior.split_long_replies = False
    assert reply_parts("第一句。第二句。第三句。", settings) == ["第一句。第二句。第三句。"]


def test_each_split_part_rechecks_permission_and_stops_after_partial_failure(tmp_path):
    s = build_services(tmp_path)
    s.config.current.qq.enabled = True
    sent = []

    async def sender(event, text):
        sent.append(text)
        s.config.current.qq.enabled = False

    s.agent.executor.sender = sender
    event = ChatEvent("34567", "23456", "split", "", directed=True)
    assert not asyncio.run(s.agent.executor.send(event, "第一句。第二句。第三句。", "direct"))
    assert sent == ["第一句。第二句。"]


@pytest.mark.parametrize("valid", [True, False])
def test_verify_native_tool_capability_only_enables_after_actual_function(tmp_path, valid):
    from soulbot.providers.llm.base import ProviderError
    from soulbot.services.config_service import ConfigService

    s = build_services(tmp_path)
    s.config.current.llm.enabled = True
    s.config.current.llm.model = "fixture"
    s.config.current.llm.supports_tools = False
    s.secrets.save({"llm_api_key": "fixture-secret"})

    def handler(request):
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "capability_probe"
        message = {"content": "plain text"}
        if valid:
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "test",
                        "type": "function",
                        "function": {"name": "capability_probe", "arguments": '{"word":"ok"}'},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    s.llm.transport = httpx.MockTransport(handler)
    if valid:
        assert asyncio.run(s.llm.verify_tools())["ok"]
        assert s.config.current.llm.supports_tools
        assert ConfigService(tmp_path).current.llm.supports_tools
    else:
        with pytest.raises(ProviderError, match="未返回有效工具调用"):
            asyncio.run(s.llm.verify_tools())
        assert not s.config.current.llm.supports_tools
    with s.db.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM llm_usage WHERE purpose='admin_capability_test'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("caption_failure", [False, True])
def test_qq_model_image_tool_to_actual_executor(tmp_path, caption_failure):
    from soulbot.providers.image.base import GeneratedImage

    s = build_services(tmp_path)
    s.config.current.llm.enabled = True
    s.config.current.llm.supports_tools = True
    s.config.current.qq.enabled = True
    s.config.current.llm.model = "fixture"
    s.config.current.llm.retries = 0
    s.config.current.meme.enabled = False
    s.config.current.emoji.enabled = False
    s.config.current.image_generation = config()
    s.secrets.save({"llm_api_key": "fixture-secret"})
    calls, sent = [], []

    def handler(request):
        body = json.loads(request.content)
        assert "generate_image" in [t["function"]["name"] for t in body["tools"]]
        assert "本轮实际可用工具：generate_image" in body["messages"][0]["content"]
        calls.append(body)
        if len(calls) == 1:
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "draw",
                        "type": "function",
                        "function": {
                            "name": "generate_image",
                            "arguments": '{"prompt":"cat portrait"}',
                        },
                    }
                ],
            }
        else:
            assert body["messages"][-1]["role"] == "tool"
            assert "attachment" in json.loads(body["messages"][-1]["content"])
            if caption_failure:
                return httpx.Response(503)
            message = {"content": "这是我的样子喵。"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 15},
            },
        )

    async def image(prompt, purpose):
        assert prompt == "cat portrait"
        return GeneratedImage(png(), "image/png", 512, 512)

    async def sender(event, payload):
        sent.append(payload)

    s.llm.transport = httpx.MockTransport(handler)
    s.images.generate = image
    s.agent.executor.sender = sender
    event = ChatEvent("34567", "23456", uuid4().hex, "直接画就行了", directed=True)
    assert asyncio.run(s.agent.process([(event, "direct")]))
    assert len(calls) == 2
    assert len(sent) == (1 if caption_failure else 2) and sent[-1].data == png()


def test_native_probe_does_not_enable_changed_model(tmp_path):
    from soulbot.providers.llm.base import ProviderError

    s = build_services(tmp_path)
    s.config.current.llm.enabled = True
    s.config.current.llm.model = "fixture"
    s.secrets.save({"llm_api_key": "fixture-secret"})

    def handler(request):
        s.config.current.llm.model = "changed"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "test",
                                    "function": {
                                        "name": "capability_probe",
                                        "arguments": '{"word":"ok"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    s.llm.transport = httpx.MockTransport(handler)
    with pytest.raises(ProviderError, match="配置发生变化"):
        asyncio.run(s.llm.verify_tools())
    assert not s.config.current.llm.supports_tools


def test_owner_retry_api_validates_source_and_never_replays_other_users(tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from soulbot.app import create_web_app

    s = build_services(tmp_path)
    s.config.current.qq.enabled = True
    s.config.current.personality.owner_qq = "23456"
    s.qq_transport = SimpleNamespace(connected=lambda: ["34567"])
    s.agent.conversation.remember("34567:p:23456", "owner request", "old refusal", s.config.current)
    s.agent.conversation.remember("34567:p:45678", "other request", "reply", s.config.current)
    with s.db.connect() as db:
        owner = db.execute("SELECT id FROM chat_context WHERE content='owner request'").fetchone()[
            0
        ]
        other = db.execute("SELECT id FROM chat_context WHERE content='other request'").fetchone()[
            0
        ]
    seen = []

    async def process(batch):
        seen.append(batch[0][0])
        return True

    s.agent.process = process
    client = TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"})
    assert (
        client.post("/api/qq/retry-last", json={"confirm": True, "source_id": owner}).status_code
        == 401
    )
    client.post(
        "/api/auth/setup", json={"username": "tester", "password": "test-password-at-least-12"}
    )
    assert (
        client.post("/api/qq/retry-last", json={"confirm": True, "source_id": other}).status_code
        == 404
    )
    assert (
        client.post("/api/qq/retry-last", json={"confirm": True, "source_id": owner}).status_code
        == 200
    )
    assert (
        client.post("/api/qq/retry-last", json={"confirm": True, "source_id": owner}).status_code
        == 429
    )
    assert len(seen) == 1 and seen[0].text == "owner request" and seen[0].user_id == "23456"


def test_task_budget_settles_measured_tokens_before_next_tool_round(tmp_path):
    s = build_services(tmp_path)
    s.config.current.llm.enabled = True
    s.config.current.llm.model = "fixture"
    s.config.current.llm.retries = 0
    s.config.current.limits.max_per_agent_task = 1500
    s.config.current.limits.max_input_per_request = 1200
    s.secrets.save({"llm_api_key": "fixture-secret"})
    s.llm.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            },
        )
    )
    state = {}
    messages = [
        {"role": "system", "content": "context" * 100},
        {"role": "user", "content": "hello"},
    ]

    async def check():
        await s.llm.complete(messages, task_state=state)
        assert state["tokens"] == 70
        await s.llm.complete(messages, task_state=state)
        assert state["tokens"] == 140

    asyncio.run(check())
