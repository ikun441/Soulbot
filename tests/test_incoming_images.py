import asyncio
import base64
import json
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageColor

from soulbot.app import build_services, create_web_app
from soulbot.core.events import ChatEvent, IncomingImage
from soulbot.providers.llm.base import (
    Completion,
    ProviderError,
    estimate_tokens,
    text_content,
    trim_context,
)
from soulbot.providers.llm.openai_compatible import OpenAICompatibleProvider
from soulbot.services.config_service import LLMEndpoint
from soulbot.services.incoming_images import content_part, download_image, platform_url
from soulbot.services.media_service import MediaError
from soulbot.services.vision_probe import verify_vision


def png(size=(80, 40), color="red"):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def s(tmp_path):
    s = build_services(tmp_path)
    cfg = s.config.current
    cfg.qq.enabled = True
    cfg.vision.enabled = True
    cfg.llm.model = "fixture-vision"
    cfg.llm.retries = 0
    cfg.llm.supports_vision = True
    cfg.behavior.global_send_interval_seconds = 0.1
    s.secrets.save({"llm_api_key": "image-fixture-key"})
    return s


def event(**changes):
    base = ChatEvent(
        "34567",
        "23456",
        "picture-1",
        "",
        images=(IncomingImage("abcdef.png", "https://gchat.qpic.cn/fixture?rkey=private-marker"),),
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/image",
        "file:///tmp/image.png",
        "https://gchat.qpic.cn.evil.invalid/i",
        "https://gchat.qpic.cn@127.0.0.1/i",
        "https://gchat.qpic.cn:1234/i",
        "https://evil.invalid/",
        "https://multimedia.nt.qq.com.cn.evil.invalid/i",
        "https://gchat.qpic.cn/#secret",
        None,
    ],
)
def test_only_qq_platform_image_addresses_are_accepted(url):
    assert not platform_url(url)
    with pytest.raises(MediaError):
        asyncio.run(download_image(url))


def test_download_actual_bytes_limit_redirect_and_bad_image(monkeypatch):
    import soulbot.services.incoming_images as module

    url = "https://multimedia.nt.qq.com.cn/download?rkey=fixture"
    assert platform_url(url)
    for response in [
        httpx.Response(302, headers={"location": "http://127.0.0.1/private"}),
        httpx.Response(200, content=b"not an image"),
    ]:
        with pytest.raises(MediaError):
            asyncio.run(download_image(url, httpx.MockTransport(lambda request: response)))
    assert (
        asyncio.run(
            download_image(
                url, httpx.MockTransport(lambda request: httpx.Response(200, content=png()))
            )
        )
        == png()
    )
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(MediaError, match="16MB"):
        asyncio.run(
            download_image(
                url, httpx.MockTransport(lambda request: httpx.Response(200, content=png()))
            )
        )


def test_resize_strips_metadata_animation_and_base64_is_not_text_tokens():
    image = Image.new("RGB", (2048, 512), "red")
    exif = Image.Exif()
    exif[0x010E] = "private metadata"
    output = BytesIO()
    image.save(output, format="JPEG", exif=exif)
    part = content_part(output.getvalue(), 512)
    raw = base64.b64decode(part["image_url"]["url"].split(",")[1])
    with Image.open(BytesIO(raw)) as result:
        assert result.size == (512, 128) and not result.getexif()
    messages = [{"role": "user", "content": [{"type": "text", "text": "看看图片"}, part]}]
    assert text_content(messages[0]) == "看看图片"
    assert 2048 < estimate_tokens(messages) < 2500
    assert "base64" not in text_content(messages[0])
    assert trim_context(messages, 2500) == messages
    with pytest.raises(ProviderError):
        trim_context(messages, 1000)
    animation = BytesIO()
    Image.new("RGB", (20, 20), "red").save(
        animation, format="GIF", save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")]
    )
    first = content_part(animation.getvalue())
    with Image.open(BytesIO(base64.b64decode(first["image_url"]["url"].split(",")[1]))) as frame:
        assert frame.getpixel((10, 10))[0] > 240


def test_normalize_preserves_image_only_message_and_bounds_attachments(s):
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

    from soulbot.adapters.onebot_v11 import normalize

    message = Message(
        [
            MessageSegment.at("34567"),
            *[
                MessageSegment("image", {"file": "hash.png", "url": "https://gchat.qpic.cn/i"})
                for _ in range(10)
            ],
        ]
    )
    raw = GroupMessageEvent(
        time=1,
        self_id=34567,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=1,
        user_id=23456,
        group_id=56789,
        message=message,
        original_message=message,
        raw_message="[CQ:image]",
        font=0,
        sender={"user_id": 23456, "nickname": "fixture", "role": "member"},
        to_me=True,
    )
    result = normalize(raw)
    assert len(result.images) == 5 and result.images[0].file_id == "hash.png"
    assert result.text == "" and s.agent.decision.classify(s.config.current, result) == "direct"
    assert s.agent.decision.classify(s.config.current, replace(result, directed=False)) is None


def test_napcat_refresh_uses_opaque_id_and_rejects_local_file(monkeypatch):
    import nonebot

    import soulbot.services.incoming_images as module
    from soulbot.adapters.onebot_v11 import NoneBotTransport

    calls = []

    class Bot:
        async def call_api(self, name, **params):
            calls.append((name, params))
            return {"url": "https://gchat.qpic.cn/refreshed", "file": "/must/not/read"}

    async def download(url):
        assert url == "https://gchat.qpic.cn/refreshed"
        return png()

    monkeypatch.setattr(nonebot, "get_bot", lambda bot_id: Bot())
    monkeypatch.setattr(module, "download_image", download)
    transport = NoneBotTransport()
    assert asyncio.run(transport.fetch_image(event(), IncomingImage("abcdef.png"))) == png()
    assert calls == [("get_image", {"file_id": "abcdef.png"})]
    for value in ["../secret", "file:///secret", "http://localhost/private"]:
        with pytest.raises(MediaError):
            asyncio.run(transport.fetch_image(event(), IncomingImage(value)))
    assert len(calls) == 1


def test_vision_route_skips_text_only_primary_and_keeps_measured_usage(s):
    cfg = s.config.current
    cfg.llm.supports_vision = False
    cfg.llm.fallbacks = {
        "fallback_1": LLMEndpoint(
            model="fixture-vision", base_url="https://backup.invalid/v1", supports_vision=True
        )
    }
    s.secrets.save({"llm_fallback_1": "backup-fixture"})
    calls = []

    def respond(request):
        calls.append(request)
        value = json.loads(request.content)
        assert value["messages"][0]["content"][1]["type"] == "image_url"
        assert request.url.host == "backup.invalid"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "红色"}}],
                "usage": {"prompt_tokens": 85, "completion_tokens": 2},
            },
        )

    s.llm.transport = httpx.MockTransport(respond)
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "什么颜色"}, content_part(png())]}
    ]
    result = asyncio.run(s.llm.complete(messages, tools=[], include_memory=False))
    assert result.provider == "fallback_1" and result.usage.input_tokens == 85
    assert len(calls) == 1
    cfg.llm.fallbacks["fallback_1"].supports_vision = False
    with pytest.raises(ProviderError, match="能力"):
        asyncio.run(s.llm.complete(messages, tools=[], include_memory=False))
    assert len(calls) == 1
    with pytest.raises(ProviderError, match="图片理解能力"):
        asyncio.run(OpenAICompatibleProvider(cfg.llm, "fixture").complete(messages, 32))


@pytest.mark.parametrize(
    "case", ["success", "disabled", "no_capability", "fetch_failed", "revoked", "filtered"]
)
def test_qq_image_pipeline_filtering_delivery_and_no_private_url_storage(s, monkeypatch, case):
    captured, sent, fetches = [], [], []
    e = event()
    if case == "disabled":
        s.config.current.vision.enabled = False
    if case == "no_capability":
        s.config.current.llm.supports_vision = False
    if case == "filtered":
        e = replace(e, group_id="56789", directed=False)

    async def fetch(evt, ref):
        fetches.append(ref)
        if case == "fetch_failed":
            raise MediaError("expired fixture")
        if case == "revoked":
            s.config.current.vision.enabled = False
        return png()

    s.qq_transport = SimpleNamespace(fetch_image=fetch)

    async def answer(messages, evt, purpose):
        captured.append(messages)
        return Completion("这是一张红色图片。", None), []

    async def sender(evt, payload):
        sent.append(payload)

    monkeypatch.setattr(s.agent, "answer", answer)
    s.agent.executor.sender = sender
    asyncio.run(s.agent.process([(e, "direct")]))
    if case == "success":
        assert len(captured) == 1 and len(fetches) == 1
        assert isinstance(captured[0][-1]["content"], list)
        assert "红色" in sent[0]
        history = s.agent.conversation.history(e.scope, 8)
        assert "[收到图片]" in history[0]["content"]
    elif case == "filtered":
        assert not captured and not sent and not fetches
    else:
        assert not captured and sent and "图片" in sent[0]
        if case in {"disabled", "no_capability"}:
            assert not fetches
    with s.db.connect() as db:
        for table in ["chat_context", "metadata", "audit"]:
            assert "private-marker" not in str(
                [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            )
            assert "data:image" not in str(
                [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            )


def test_max_images_partial_failures_and_cancellation(s, monkeypatch):
    from soulbot.services.incoming_images import prepare_images

    s.config.current.vision.max_images = 2
    e = event(images=tuple(IncomingImage(str(i)) for i in range(4)))
    seen = []

    async def fetch(evt, ref):
        seen.append(ref.file_id)
        if ref.file_id == "0":
            raise MediaError("expired")
        return png()

    s.qq_transport = SimpleNamespace(fetch_image=fetch)
    parts, notice = asyncio.run(prepare_images(s.agent, [(e, "direct")]))
    assert len(parts) == 1 and seen == ["0", "1"]
    assert "1 张图片下载失败" in notice and "前 2 张" in notice

    async def cancelled(evt, ref):
        raise asyncio.CancelledError

    s.qq_transport.fetch_image = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(prepare_images(s.agent, [(e, "direct")]))


def probe_response(request):
    value = json.loads(request.content)
    assert value["messages"][0]["content"][1]["image_url"]["detail"] == "low"
    image = Image.open(
        BytesIO(
            base64.b64decode(value["messages"][0]["content"][1]["image_url"]["url"].split(",")[1])
        )
    )
    palette = ["red", "blue", "green", "yellow", "black", "white", "purple", "orange"]

    def closest(pixel):
        return min(
            palette,
            key=lambda name: sum(
                (a - b) ** 2 for a, b in zip(pixel, ImageColor.getrgb(name), strict=True)
            ),
        )

    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "left": closest(image.getpixel((100, 100))),
                                "right": closest(image.getpixel((400, 100))),
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 15},
        },
    )


def test_real_probe_wire_format_enables_only_on_correct_image_and_unchanged_config(s):
    s.config.current.llm.supports_vision = False
    s.llm.transport = httpx.MockTransport(probe_response)
    assert asyncio.run(verify_vision(s.llm))["ok"]
    assert s.config.current.llm.supports_vision
    s.config.current.llm.supports_vision = False

    def wrong(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "连接成功"}}]})

    s.llm.transport = httpx.MockTransport(wrong)
    with pytest.raises(ProviderError, match="未正确识别"):
        asyncio.run(verify_vision(s.llm))
    assert not s.config.current.llm.supports_vision

    def changed(request):
        result = probe_response(request)
        s.config.current.llm.model = "changed-during-request"
        return result

    s.llm.transport = httpx.MockTransport(changed)
    with pytest.raises(ProviderError, match="配置发生变化"):
        asyncio.run(verify_vision(s.llm))
    assert not s.config.current.llm.supports_vision


def test_vision_test_api_requires_login_and_records_configuration(s):
    s.llm.transport = httpx.MockTransport(probe_response)
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as client:
        assert client.post("/api/llm/vision-test").status_code == 401
        client.post(
            "/api/auth/setup",
            json={"username": "vision_admin", "password": "vision-fixture-password"},
        )
        assert client.post("/api/llm/vision-test").json()["ok"]
        cfg = client.get("/api/config").json()["config"]
        assert cfg["llm"]["supports_vision"]
        cfg["vision"] = {"enabled": True, "max_images": 4, "max_side": 512}
        saved = client.put("/api/config", json=cfg)
        assert saved.status_code == 200, saved.text
        assert client.get("/api/config").json()["config"]["vision"]["max_images"] == 4
        cfg["vision"]["max_images"] = 5
        assert client.put("/api/config", json=cfg).status_code == 422
