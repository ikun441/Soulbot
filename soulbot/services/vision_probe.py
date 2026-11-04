"""A harmless, randomized real visual capability probe; never enable on text alone."""

import asyncio
import json
import re
import secrets
from io import BytesIO
from types import SimpleNamespace

from PIL import Image, ImageDraw

from soulbot.providers.llm.base import ProviderError
from soulbot.providers.registry import ProviderRegistry
from soulbot.services.incoming_images import content_part


async def verify_vision(service):
    from soulbot.services.llm_service import LLMService

    original, credentials = service.test_inputs()
    snapshot = service.config.current.model_copy(deep=True)
    snapshot.llm.supports_vision = True
    snapshot.llm.retries = 0
    snapshot.llm.fallbacks = {}
    snapshot.vision.enabled = True
    palette = ["red", "blue", "green", "yellow", "black", "white", "purple", "orange"]
    left = secrets.choice(palette)
    right = secrets.choice([c for c in palette if c != left])
    image = Image.new("RGB", (512, 256), left)
    ImageDraw.Draw(image).rectangle((256, 0, 511, 255), fill=right)
    stream = BytesIO()
    image.save(stream, format="PNG")
    probe = LLMService(
        SimpleNamespace(current=snapshot),
        service.secrets,
        ProviderRegistry(),
        service.budget,
        service.audit,
    )
    probe.transport = service.transport
    result = await probe.complete(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "识别图片左右两个色块的颜色。只输出 JSON，left/right 对应颜色的英文小写名称。",
                    },
                    content_part(stream.getvalue()),
                ],
            }
        ],
        purpose="admin_vision_test",
        tools=[],
        output_limit=128,
        include_memory=False,
    )
    text = result.text.strip()
    match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    try:
        valid = not result.tool_calls and json.loads(match.group(1) if match else text) == {
            "left": left,
            "right": right,
        }
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderError(
            "vision_capability",
            "模型未正确识别测试图片；视觉能力开关保持原样，请检查模型与接口兼容性",
        )

    def apply():
        with service.config.lock:
            current, current_credentials = service.test_inputs()
            if current != original or current_credentials != credentials:
                raise ProviderError(
                    "config_changed", "测试期间模型配置发生变化，请重新验证；未启用旧配置"
                )
            settings = service.config.current.model_copy(deep=True)
            settings.llm.supports_vision = True
            service.config.save(settings)
            service.audit("llm_vision_verified", result.provider)

    await asyncio.to_thread(apply)
    return {
        "ok": True,
        "message": "已通过随机图片识别并启用主模型视觉能力；可开启 QQ 图片理解，本次测试已记录费用。",
        "model": result.model,
    }
