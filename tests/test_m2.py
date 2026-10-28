import asyncio
import base64
import json
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from soulbot.adapters.onebot_v11 import NoneBotTransport
from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent
from soulbot.providers.llm.base import ProviderError, estimate_tokens, trim_context
from soulbot.services.media_service import ImagePayload, MediaError, inspect_image
from soulbot.services.meme_service import InstanceLock, MemeClient


def png():
    buffer = BytesIO()
    Image.new("RGB", (32, 24), "pink").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def services(tmp_path):
    return build_services(tmp_path)


@pytest.fixture
def client(tmp_path, services):
    with TestClient(
        create_web_app(tmp_path, services), headers={"X-Soulbot-Request": "1"}
    ) as client:
        assert (
            client.post(
                "/api/auth/setup",
                json={"username": "m2_admin", "password": "isolated-test-password-123"},
            ).status_code
            == 200
        )
        yield client


def test_media_api_requires_authentication(tmp_path):
    with TestClient(create_web_app(tmp_path), headers={"X-Soulbot-Request": "1"}) as client:
        for method, path in [
            ("GET", "/api/emoji"),
            ("GET", "/api/media/" + "a" * 32),
            ("GET", "/api/meme/status"),
            ("POST", "/api/emoji/refresh"),
            ("POST", "/api/meme/test"),
            ("GET", "/api/tools"),
        ]:
            assert client.request(method, path).status_code == 401


def test_emoji_chinese_recursive_tags_pagination_and_removal(services):
    library = services.emoji
    nested = library.root / "开心" / "子分类"
    nested.mkdir()
    (nested / "今天好开心.png").write_bytes(png())
    for index in range(5):
        (library.root / "整活" / f"反应{index}.png").write_bytes(png())
    assert library.refresh()["total"] == 6
    item = library.search("开心")["items"][0]
    assert item["name"] == "今天好开心.png"
    library.update(item["id"], ["快乐"], ["笑一个"])
    assert library.search("笑一个")["items"][0]["id"] == item["id"]
    first = library.browse(0, 2)
    second = library.browse(2, 2)
    assert {row["id"] for row in first["items"]}.isdisjoint(row["id"] for row in second["items"])
    assert first["total"] == 6
    assert library.read(item["id"]).mime == "image/png"
    (nested / "今天好开心.png").unlink()
    library.refresh()
    with pytest.raises(MediaError):
        library.get(item["id"])


def test_emoji_symlinks_and_path_traversal_are_blocked(services, tmp_path):
    private = tmp_path / "private.png"
    private.write_bytes(png())
    (services.emoji.root / "escape.png").symlink_to(private)
    assert services.emoji.refresh()["total"] == 0
    with pytest.raises(MediaError):
        services.emoji.path("../private.png")
    with pytest.raises(MediaError):
        services.emoji.upload("../bad.png", png(), "其他")
    with pytest.raises(MediaError):
        services.emoji.upload("safe.png", png(), "../其他")


def test_emoji_upload_and_private_preview_are_real(client):
    response = client.post(
        "/api/emoji/upload",
        data={"category": "开心"},
        files={"file": ("桃桃.png", png(), "image/png")},
    )
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["name"] == "桃桃.png"
    assert "path" not in item
    assert client.get(item["url"]).content == png()
    assert (
        client.put(
            f"/api/emoji/{item['id']}/labels", json={"tags": ["开心"], "aliases": ["桃子"]}
        ).status_code
        == 200
    )
    assert client.get("/api/emoji?query=桃子").json()["items"][0]["id"] == item["id"]
    assert (
        client.post(
            "/api/emoji/upload", files={"file": ("bad.png", b"not an image", "image/png")}
        ).status_code
        == 422
    )


def test_generated_image_storage_validates_format_and_expiry(services):
    item = services.media.save(png())
    assert (item["mime"], item["width"], item["height"]) == ("image/png", 32, 24)
    assert services.media.read(item["id"]).data == png()
    with pytest.raises(MediaError):
        services.media.read("../../secrets.json")
    with pytest.raises(MediaError):
        services.media.save(b"<svg>untrusted</svg>")
    with services.db.connect() as db:
        db.execute("UPDATE media_artifacts SET expires=0")
    with pytest.raises(MediaError):
        services.media.read(item["id"])
    services.media.cleanup()
    assert not list(services.media.directory.iterdir())


def test_generated_images_are_isolated_between_qq_sessions(services):
    scope = "bot:p:alice"
    item = services.media.save(png(), "meme", scope)
    assert "scope" not in item
    assert services.media.read(item["id"], scope).data == png()
    with pytest.raises(MediaError, match="其它会话"):
        services.media.read(item["id"], "bot:g:public:u:alice")
    with pytest.raises(MediaError, match="其它会话"):
        services.media.read(item["id"], "bot:p:bob")


def metadata(key="raise_sign"):
    return {
        "key": key,
        "keywords": ["举牌"],
        "params_type": {
            "min_images": 0,
            "max_images": 0,
            "min_texts": 1,
            "max_texts": 1,
            "default_texts": ["hi"],
            "args_type": None,
        },
    }


def test_meme_client_uses_verified_multipart_and_checks_counts(services):
    services.config.current.meme.enabled = True
    services.config.current.meme.managed = False
    client = MemeClient(services.config)
    generated = []

    def respond(request):
        if request.url.path == "/meme/version":
            return httpx.Response(200, json="0.1.14")
        if request.url.path == "/memes/keys":
            return httpx.Response(200, json=["raise_sign"])
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json=metadata())
        generated.append(request)
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert b'name="texts"' in request.content and "你好".encode() in request.content
        assert b'name="args"' in request.content
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    client.transport = httpx.MockTransport(respond)

    async def check():
        assert (await client.health())["templates"] == 1
        assert await client.generate("raise_sign", ["你好"], []) == png()
        with pytest.raises(MediaError, match="段文字"):
            await client.generate("raise_sign", [], [])
        with pytest.raises(MediaError):
            await client.info("../keys")

    asyncio.run(check())
    assert len(generated) == 1


def test_meme_rejects_external_schema_references_and_unknown_args(services):
    services.config.current.meme.enabled = True
    client = MemeClient(services.config)
    info = metadata()
    info["params_type"]["args_type"] = {"args_model": {"$ref": "https://invalid.example/schema"}}
    client.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=info))
    with pytest.raises(MediaError, match="外部"):
        asyncio.run(client.generate("raise_sign", ["hi"], [], {"option": True}))


def test_single_instance_lock_is_released_after_close(tmp_path):
    first, second = InstanceLock(tmp_path / "service.lock"), InstanceLock(tmp_path / "service.lock")
    assert first.acquire()
    assert not second.acquire()
    first.close()
    assert second.acquire()
    second.close()


def test_media_send_has_no_arbitrary_recipient_or_url(client):
    assert (
        client.post(
            "/api/media/send-test", json={"confirm": False, "image": "media:" + "a" * 32}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/media/send-test", json={"confirm": True, "image": "https://outside.example/a.png"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/media/send-test",
            json={"confirm": True, "image": "media:" + "a" * 32, "recipient": "123456"},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("revoke", [False, True])
def test_agent_selects_emoji_through_tools_and_rechecks_permission(services, revoke):
    settings = services.config.current.model_copy(deep=True)
    settings.qq.enabled = True
    settings.llm.model = "tool-fixture"
    settings.llm.supports_tools = True
    settings.llm.retries = 0
    settings.behavior.private_batch_seconds = 0
    services.secrets.save({"llm_api_key": "fixture-key", "onebot_access_token": "fixture-token"})
    services.config.save(settings)
    item = services.emoji.upload("笑脸.png", png(), "开心")
    calls, sent = [], []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            assert "select_emoji" in [t["function"]["name"] for t in body["tools"]]
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {
                            "name": "select_emoji",
                            "arguments": json.dumps({"emoji_id": item["id"]}),
                        },
                    }
                ],
            }
        else:
            assert body["messages"][-1]["role"] == "tool"
            if revoke:
                services.config.current.tools.select_emoji = "deny"
            message = {"content": "[[SILENT]]"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5},
            },
        )

    services.llm.transport = httpx.MockTransport(respond)

    async def sender(event, content):
        sent.append(content)

    services.agent.executor.sender = sender

    async def check():
        await services.agent.accept(ChatEvent("10001", "20002", "m2-tool-test", "发个表情"))
        async with asyncio.timeout(5):
            while services.agent.pending:
                await asyncio.sleep(0.01)
        await services.agent.close()

    asyncio.run(check())
    assert len(calls) == 2
    assert len(sent) == (0 if revoke else 1)
    if sent:
        assert isinstance(sent[0], ImagePayload) and sent[0].data == png()


def test_tool_turns_share_task_budget(services):
    services.config.current.llm.model = "fixture"
    services.config.current.llm.retries = 0
    services.config.current.limits.max_per_agent_task = 1000
    services.secrets.save({"llm_api_key": "fixture"})
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 600, "completion_tokens": 1},
            },
        )

    services.llm.transport = httpx.MockTransport(respond)

    async def check():
        state = {}
        messages = [{"role": "user", "content": "hello"}]
        await services.llm.complete(messages, task_state=state)
        assert state["tokens"] == 601
        with pytest.raises(ProviderError, match="本次任务"):
            await services.llm.complete(messages, task_state=state)

    asyncio.run(check())
    assert len(requests) == 1


def test_tool_schema_is_counted_before_provider_request(services):
    services.config.current.llm.model = "fixture"
    services.config.current.llm.supports_tools = True
    services.config.current.limits.max_input_per_request = 256
    services.secrets.save({"llm_api_key": "fixture"})
    specs = [
        {
            "type": "function",
            "function": {
                "name": "large",
                "description": "x" * 500,
                "parameters": {"type": "object"},
            },
        }
    ]
    with pytest.raises(ProviderError, match="输入内容过长"):
        asyncio.run(services.llm.complete([{"role": "user", "content": "hi"}], tools=specs))
    assert services.budget.summary(services.config.current)["reserved_or_used_tokens"] == 0


def test_missing_third_party_preparation_does_not_break_core(services, client):
    services.config.current.meme.enabled = True
    services.config.current.meme.auto_install = False

    async def check():
        await services.meme.begin(install=False)
        await services.meme.task
        assert services.meme.status()["phase"] == "failed"
        assert services.meme.lock.stream is None
        await services.meme.close()

    asyncio.run(check())
    assert client.get("/api/health").json()["sqlite"]


def test_context_trimming_does_not_split_current_tool_chain():
    current = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "now"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c"}]},
        {"role": "tool", "tool_call_id": "c", "content": "result"},
    ]
    full = [
        current[0],
        {"role": "user", "content": "old" * 500},
        {"role": "assistant", "content": "old response"},
        *current[1:],
    ]
    assert trim_context(full, estimate_tokens(current) + 10) == current


def test_image_format_metadata_is_derived_from_bytes():
    assert inspect_image(png()) == ("png", "image/png", 32, 24)
    with pytest.raises(MediaError):
        inspect_image(b"not-an-image")


def test_onebot_transport_sends_image_segment_with_base64(monkeypatch):
    calls = []

    class Bot:
        async def send_private_msg(self, **kwargs):
            calls.append(kwargs)
            return {"message_id": 42}

    monkeypatch.setattr("nonebot.get_bot", lambda bot_id: Bot())
    event = ChatEvent("10001", "20002", "fixture", "")
    asyncio.run(NoneBotTransport()(event, ImagePayload(png(), "image/png")))
    segment = calls[0]["message"]
    assert calls[0]["user_id"] == 20002
    assert segment.type == "image"
    assert base64.b64decode(segment.data["file"].removeprefix("base64://")) == png()


def test_image_test_send_is_fixed_to_owner_and_rate_limited(services, client):
    services.config.current.qq.enabled = True
    services.config.current.personality.owner_qq = "123456789"
    services.qq_transport = type("Transport", (), {"connected": lambda self: ["10001"]})()
    item = services.media.save(png())
    sent = []

    async def sender(event, payload):
        sent.append((event.user_id, payload))

    services.agent.executor.sender = sender
    payload = {"confirm": True, "image": "media:" + item["id"]}
    assert client.post("/api/media/send-test", json=payload).status_code == 200
    assert sent[0][0] == "123456789" and isinstance(sent[0][1], ImagePayload)
    assert client.post("/api/media/send-test", json=payload).status_code == 429
    assert len(sent) == 1
