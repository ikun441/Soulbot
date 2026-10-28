"""Local ComfyUI fixed checkpoint workflow. Model input can change only positive text."""

import asyncio
import secrets
import time
from pathlib import PurePosixPath
from uuid import UUID, uuid4

import httpx

from soulbot.providers.image.base import GeneratedImage, ImageGenerationError
from soulbot.services.media_service import MAX_IMAGE_BYTES, inspect_image


class ComfyUIProvider:
    capabilities = frozenset({"image_generation"})

    def __init__(self):
        self.transport = None
        self.lock = asyncio.Lock()
        self.last_success = 0

    async def request(self, client, method, url, *, maximum=1048576, **kwargs):
        async with client.stream(method, url, **kwargs) as response:
            if response.status_code != 200:
                raise ImageGenerationError(
                    "parameters" if response.status_code in {400, 404, 422} else "unavailable",
                    "ComfyUI 请求未成功，请核对节点、模型与队列",
                    method == "POST" or response.status_code >= 500,
                )
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > maximum:
                    raise ImageGenerationError("response", "ComfyUI 响应超过大小限制", True)
            return bytes(content)

    async def probe(self, settings):
        import json

        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=10
            ) as client:
                system = json.loads(
                    await self.request(client, "GET", settings.base_url + "/system_stats")
                )
                checkpoint = json.loads(
                    await self.request(
                        client, "GET", settings.base_url + "/object_info/CheckpointLoaderSimple"
                    )
                )
                sampler = json.loads(
                    await self.request(client, "GET", settings.base_url + "/object_info/KSampler")
                )
            names = checkpoint["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
            inputs = sampler["KSampler"]["input"]["required"]
            return {
                "ok": True,
                "comfyui_version": system.get("system", {}).get("comfyui_version"),
                "checkpoints": names[:500],
                "samplers": inputs["sampler_name"][0],
                "schedulers": inputs["scheduler"][0],
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ImageGenerationError(
                "configuration",
                "无法读取本地 ComfyUI 及基础模型节点，请检查地址、启动和 checkpoint 模型",
            ) from None

    @staticmethod
    def workflow(prompt, settings, prefix):
        cfg = settings.comfy
        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": secrets.randbits(48),
                    "steps": cfg.steps,
                    "cfg": cfg.cfg,
                    "sampler_name": cfg.sampler,
                    "scheduler": cfg.scheduler,
                    "denoise": 1,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": cfg.checkpoint}},
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": cfg.width, "height": cfg.height, "batch_size": 1},
            },
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": cfg.negative_prompt, "clip": ["4", 1]},
            },
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": prefix, "images": ["8", 0]},
            },
        }

    async def generate(self, prompt, settings):
        import json

        cfg = settings.comfy
        prompt_id = None
        async with self.lock:
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=min(30, settings.timeout_seconds),
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    capabilities = await self.probe(cfg)
                    if (
                        cfg.checkpoint not in capabilities["checkpoints"]
                        or cfg.sampler not in capabilities["samplers"]
                        or cfg.scheduler not in capabilities["schedulers"]
                    ):
                        raise ImageGenerationError(
                            "parameters", "所选模型、采样器或调度器不在 ComfyUI 当前实际列表中"
                        )
                    prefix = "soulbot_" + uuid4().hex
                    submitted = json.loads(
                        await self.request(
                            client,
                            "POST",
                            cfg.base_url + "/prompt",
                            json={
                                "prompt": self.workflow(prompt, settings, prefix),
                                "client_id": uuid4().hex,
                            },
                        )
                    )
                    prompt_id = submitted.get("prompt_id")
                    if (
                        not isinstance(prompt_id, str)
                        or len(prompt_id) > 100
                        or submitted.get("node_errors")
                    ):
                        raise ImageGenerationError(
                            "parameters", "ComfyUI 拒绝此固定工作流，请核对模型和基础节点", True
                        )
                    try:
                        UUID(prompt_id)
                    except ValueError:
                        raise ImageGenerationError(
                            "response", "ComfyUI 未返回有效任务标识", True
                        ) from None
                    while True:
                        history = json.loads(
                            await self.request(
                                client, "GET", cfg.base_url + "/history/" + prompt_id
                            )
                        )
                        item = history.get(prompt_id)
                        if item:
                            if item.get("status", {}).get("status_str") == "error":
                                raise ImageGenerationError(
                                    "generation",
                                    "ComfyUI 工作流执行失败，请在本机查看节点错误",
                                    True,
                                )
                            images = item.get("outputs", {}).get("9", {}).get("images", [])
                            if images:
                                image = images[0]
                                filename, folder = (
                                    image.get("filename", ""),
                                    image.get("subfolder", ""),
                                )
                                if (
                                    not isinstance(filename, str)
                                    or PurePosixPath(filename).name != filename
                                    or not filename.startswith(prefix)
                                    or image.get("type") != "output"
                                    or not isinstance(folder, str)
                                    or PurePosixPath(folder).is_absolute()
                                    or ".." in PurePosixPath(folder).parts
                                    or "\\" in folder
                                ):
                                    raise ImageGenerationError(
                                        "response", "ComfyUI 输出文件标识不符合当前任务", True
                                    )
                                data = await self.request(
                                    client,
                                    "GET",
                                    cfg.base_url + "/view",
                                    maximum=MAX_IMAGE_BYTES,
                                    params={
                                        "filename": filename,
                                        "subfolder": folder,
                                        "type": "output",
                                    },
                                )
                                _, mime, width, height = inspect_image(data)
                                self.last_success = time.time()
                                return GeneratedImage(data, mime, width, height)
                            if item.get("status", {}).get("completed"):
                                raise ImageGenerationError(
                                    "response", "ComfyUI 完成但没有当前任务图片", True
                                )
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                # Do not call global /interrupt: that could cancel another user's running workflow.
                if prompt_id:
                    try:
                        async with httpx.AsyncClient(
                            transport=self.transport, timeout=3, trust_env=False
                        ) as client:
                            await client.post(cfg.base_url + "/queue", json={"delete": [prompt_id]})
                    except httpx.HTTPError:
                        pass
                raise
            except ImageGenerationError:
                raise
            except httpx.TimeoutException:
                raise ImageGenerationError(
                    "timeout", "本地 ComfyUI 请求超时，任务可能仍在运行", True
                ) from None
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                raise ImageGenerationError(
                    "response", "本地 ComfyUI 网络或响应无效，请检查任务历史", bool(prompt_id)
                ) from None

    async def health_check(self):
        return self.last_success > 0

    async def close(self):
        pass
