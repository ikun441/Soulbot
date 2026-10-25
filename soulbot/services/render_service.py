"""Optional project-local browser installation and bounded, owned render jobs."""

import asyncio
import hashlib
import json
import os
import signal
import sys
from pathlib import Path

import httpx

from soulbot.services.config_service import atomic_write
from soulbot.services.media_service import MAX_IMAGE_BYTES, MediaError, inspect_image
from soulbot.services.meme_service import InstanceLock, MemeService
from soulbot.tools.html_render import FONT_SHA256, FONT_URL, CardRequest

PLAYWRIGHT_VERSION = "1.64.1"
PROJECT = Path(__file__).resolve().parents[2]
WORKER = PROJECT / "scripts" / "render_worker.py"


class HTMLRenderService:
    capabilities = frozenset({"html_render"})

    def __init__(self, root, config, audit):
        self.root = root / "data" / "html_render"
        self.config, self.audit = config, audit
        self.task = None
        self.processes, self.jobs = set(), set()
        self.pending, self.closing = 0, False
        self.limiter = asyncio.Semaphore(2)
        self.lock = InstanceLock(self.root / "install.lock")
        self.phase = "ready" if self.ready() else "not_installed"
        self.message = "Chromium 已准备" if self.ready() else "先准备项目专用 Chromium 和中文字体"

    def manifest(self):
        try:
            value = json.loads((self.root / "ready.json").read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def ready(self):
        data = self.manifest()
        executable_value = data.get("executable", "")
        executable = Path(executable_value if isinstance(executable_value, str) else "")
        font = self.root / "NotoSansSC-Regular.ttf"
        return (
            not self.root.is_symlink()
            and not (self.root / "browsers").is_symlink()
            and data.get("version") == PLAYWRIGHT_VERSION
            and executable.is_file()
            and executable.resolve().is_relative_to((self.root / "browsers").resolve())
            and font.is_file()
            and not font.is_symlink()
            and data.get("font_sha256") == FONT_SHA256
        )

    def status(self):
        return {
            "phase": self.phase,
            "message": self.message,
            "ready": self.ready(),
            "enabled": self.config.current.html_render.enabled,
            "installing": bool(self.task and not self.task.done()),
            "pending": self.pending,
            "max_concurrency": 2,
            "playwright_version": PLAYWRIGHT_VERSION,
            "browser_version": self.manifest().get("browser_version"),
        }

    async def health_check(self):
        return self.ready()

    def environment(self):
        return MemeService.environment() | {
            "PLAYWRIGHT_BROWSERS_PATH": str(self.root / "browsers"),
            "PLAYWRIGHT_SKIP_BROWSER_GC": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    async def terminate(self, process):
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.wait(), 5)
            except TimeoutError:
                killer.kill()
                await killer.wait()
                if process.returncode is None:
                    process.kill()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                process.kill()
            await process.wait()

    async def command(self, command, payload=None, maximum=MAX_IMAGE_BYTES):
        process = await asyncio.create_subprocess_exec(
            *map(str, command),
            cwd=PROJECT,
            env=self.environment(),
            stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        self.processes.add(process)

        async def read(stream, limit, strict=False):
            content = bytearray()
            while chunk := await stream.read(65536):
                if strict and limit and len(content) + len(chunk) > limit:
                    raise MediaError("渲染进程输出超过大小限制")
                if limit:
                    content.extend(chunk[: max(0, limit - len(content))])
            return bytes(content)

        readers = [
            asyncio.create_task(read(process.stdout, maximum, True)),
            asyncio.create_task(read(process.stderr, 8192)),
        ]
        try:
            if payload is not None:
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
            output, error = await asyncio.gather(*readers)
            return await process.wait(), output, error
        finally:
            await self.terminate(process)
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            self.processes.discard(process)

    async def prepare_font(self):
        target = self.root / "NotoSansSC-Regular.ttf"
        if target.is_symlink():
            raise MediaError("中文字体文件不能是符号链接")
        existing = PROJECT / "third_party" / "meme-generator" / "fonts" / target.name
        for path in (target, existing):
            if path.is_file() and not path.is_symlink():
                content = await asyncio.to_thread(path.read_bytes)
                if hashlib.sha256(content).hexdigest() == FONT_SHA256:
                    if path != target:
                        temporary = target.with_suffix(".partial")
                        temporary.unlink(missing_ok=True)
                        with temporary.open("xb") as stream:
                            temporary.chmod(0o600)
                            stream.write(content)
                        os.replace(temporary, target)
                    return
        temporary = target.with_suffix(".partial")
        temporary.unlink(missing_ok=True)
        try:
            digest, size = hashlib.sha256(), 0
            async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
                async with client.stream("GET", FONT_URL) as response:
                    response.raise_for_status()
                    with temporary.open("xb") as stream:
                        temporary.chmod(0o600)
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 32 * 1024 * 1024:
                                raise MediaError("中文字体下载超出大小限制")
                            digest.update(chunk)
                            stream.write(chunk)
            if digest.hexdigest() != FONT_SHA256:
                raise MediaError("中文字体校验失败，请重试准备功能")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    async def begin_install(self):
        if self.closing:
            raise MediaError("服务正在关闭")
        if self.task and not self.task.done():
            return self.status()
        if self.pending:
            raise MediaError("请等待卡片生成任务结束后准备浏览器")
        if self.root.is_symlink() or (self.root / "browsers").is_symlink():
            raise MediaError("渲染数据目录不能是符号链接")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.lock.acquire():
            raise MediaError("另一个 SoulBot 实例正在准备同一渲染目录")
        self.phase, self.message = "installing", "正在下载项目专用 Chromium，可能需要数分钟"
        self.task = asyncio.create_task(self.install())
        return self.status()

    async def install(self):
        try:
            async with asyncio.timeout(900):
                code, _, _ = await self.command(
                    [sys.executable, "-m", "playwright", "install", "--no-shell", "chromium"],
                    maximum=0,
                )
                if code:
                    raise MediaError("Chromium 下载失败，请检查网络和磁盘空间后重试")
                self.message = "正在准备并校验中文字体"
                await self.prepare_font()
                self.message = "正在实际启动 Chromium 检查运行环境"
                code, output, _ = await self.command(
                    [sys.executable, WORKER, "--root", self.root, "--probe"], maximum=4096
                )
                if code:
                    raise MediaError(
                        "Chromium 无法启动：请检查 Linux 系统依赖或沙箱权限；未自动安装系统组件"
                    )
                data = json.loads(output)
                data["font_sha256"] = FONT_SHA256
                atomic_write(self.root / "ready.json", json.dumps(data))
                if not self.ready():
                    raise MediaError("浏览器准备结果无效，请重新准备")
                self.phase, self.message = "ready", "Chromium 与中文字体已通过实际启动检查"
                await asyncio.to_thread(self.audit, "html_render_prepared")
        except asyncio.CancelledError:
            self.phase, self.message = "cancelled", "已取消准备，可稍后重试"
            raise
        except Exception as error:
            self.phase = "failed"
            self.message = (
                str(error)
                if isinstance(error, MediaError)
                else "渲染环境准备失败，请检查网络、磁盘或 Python 依赖后重试"
            )
            await asyncio.to_thread(self.audit, "html_render_prepare_failed")
        finally:
            self.lock.close()

    async def cancel_install(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        return self.status()

    async def render(self, card: CardRequest):
        if self.closing or not self.config.current.html_render.enabled:
            raise MediaError("HTML 卡片功能尚未启用")
        if not self.ready() or (self.task and not self.task.done()):
            raise MediaError("请先完成 Chromium 和中文字体准备")
        if self.pending >= 4:
            raise MediaError("卡片生成任务较多，请稍后重试")
        settings = self.config.current.html_render.model_copy(deep=True)
        task = asyncio.current_task()
        self.jobs.add(task)
        self.pending += 1
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with self.limiter:
                    if not self.config.current.html_render.enabled:
                        raise MediaError("HTML 卡片功能已关闭")
                    payload = json.dumps(
                        {
                            "card": card.model_dump(),
                            "width": settings.width,
                            "max_height": settings.max_height,
                        }
                    ).encode()
                    code, output, error = await self.command(
                        [sys.executable, WORKER, "--root", self.root], payload
                    )
                    if code:
                        try:
                            kind = json.loads(error)["error"]
                        except (ValueError, KeyError, TypeError):
                            kind = "runtime"
                        messages = {
                            "height": "内容超过卡片高度，请缩短正文、减少清单项目或提高高度上限",
                            "font": "中文字体校验失败，请重新准备环境",
                        }
                        raise MediaError(
                            messages.get(kind, "卡片渲染失败，请检查浏览器环境并重新准备")
                        )
                    _, mime, width, height = inspect_image(output)
                    if (
                        mime != "image/png"
                        or width != settings.width
                        or height > settings.max_height
                    ):
                        raise MediaError("渲染结果格式或尺寸不符合当前限制")
                    if not self.config.current.html_render.enabled:
                        raise MediaError("HTML 卡片功能已关闭")
                    return output
        except TimeoutError:
            raise MediaError("卡片生成超时，请减少内容或稍后重试") from None
        finally:
            self.pending -= 1
            self.jobs.discard(task)

    async def close(self):
        self.closing = True
        await self.cancel_install()
        jobs = [task for task in self.jobs if task is not asyncio.current_task()]
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for process in list(self.processes):
            await self.terminate(process)
        self.lock.close()
