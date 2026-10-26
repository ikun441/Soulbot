import asyncio
import json
import sys
from pathlib import Path

from soulbot.providers.asr.base import ASRError, Transcription
from soulbot.services.config_service import atomic_write
from soulbot.services.meme_service import InstanceLock, MemeService

REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
PROJECT = Path(__file__).resolve().parents[3]
WORKER = PROJECT / "scripts" / "asr_worker.py"
FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "README.md")


class LocalASRProvider:
    capabilities = frozenset({"asr"})

    def __init__(self, root):
        self.root = root / "data" / "asr"
        self.directory = self.root / "base"
        self.processes = set()
        self.task = None
        self.message = "首次使用请准备本地 Whisper base 模型（约 150MB）"
        self.lock = InstanceLock(self.root / "prepare.lock")

    def ready(self):
        try:
            if self.root.is_symlink() or self.directory.is_symlink():
                return False
            manifest = json.loads((self.root / "ready.json").read_text())
            return manifest["revision"] == REVISION and all(
                (self.directory / name).is_file()
                and not (self.directory / name).is_symlink()
                and (self.directory / name).stat().st_size == manifest["sizes"][name]
                for name in FILES
            )
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def environment(self, prepare=False):
        return MemeService.environment() | {
            "HF_HOME": str(self.root / "hf-cache"),
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "0" if prepare else "1",
            "HF_HUB_DISABLE_XET": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": "4",
        }

    async def command(self, payload=None, language="zh", prepare=False):
        if self.root.is_symlink() or self.directory.is_symlink():
            raise ASRError("path", "本地识别目录不能是符号链接")
        args = [sys.executable, str(WORKER), str(self.directory)]
        args += ["--prepare"] if prepare else ["--language", language]
        process = await asyncio.create_subprocess_exec(
            *args,
            env=self.environment(prepare),
            cwd=PROJECT,
            stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes.add(process)

        async def read(stream, strict):
            data = bytearray()
            while chunk := await stream.read(16384):
                if strict and len(data) + len(chunk) > 65536:
                    raise ASRError("size", "本地识别结果过大")
                data.extend(chunk[: max(0, 65536 - len(data))])
            return bytes(data)

        readers = [
            asyncio.create_task(read(process.stdout, True)),
            asyncio.create_task(read(process.stderr, False)),
        ]
        try:
            if payload is not None:
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
            output, _ = await asyncio.gather(*readers)
            if await process.wait():
                raise ASRError("local", "本地识别未完成，请检查模型和依赖，或缩短录音")
            return json.loads(output)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            self.processes.discard(process)

    async def begin_prepare(self):
        if self.task and not self.task.done():
            return
        if self.processes:
            raise ASRError("busy", "请等待当前识别结束后再准备模型")
        if self.root.is_symlink() or self.directory.is_symlink():
            raise ASRError("path", "本地识别目录不能是符号链接")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.lock.acquire():
            raise ASRError("busy", "另一个实例正在准备本地模型")
        self.task = asyncio.create_task(self.prepare())

    async def prepare(self):
        self.message = "正在下载固定版本 Whisper base 模型并检查 CPU 加载…"
        try:
            async with asyncio.timeout(900):
                result = await self.command(prepare=True)
            if not result.get("ready") or result.get("revision") != REVISION:
                raise ValueError
            manifest = {
                "revision": REVISION,
                "sizes": {name: (self.directory / name).stat().st_size for name in FILES},
            }
            atomic_write(self.root / "ready.json", json.dumps(manifest))
            self.message = "本地 Whisper base 已准备；识别时不发送录音到云端"
        except asyncio.CancelledError:
            self.message = "模型准备已取消，可以稍后重新准备"
            raise
        except Exception:
            self.message = "模型准备失败，请检查网络与依赖后重试；核心服务继续可用"
        finally:
            self.lock.close()

    async def cancel_prepare(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        # Cancellation before the first task turn skips prepare()'s finally block.
        self.lock.close()

    async def transcribe(self, audio, settings):
        if not self.ready() or (self.task and not self.task.done()):
            raise ASRError("not_ready", "请先在语音识别页面准备本地模型")
        if self.processes:
            raise ASRError("busy", "本地识别正在处理另一段录音，请稍后再试")
        result = await self.command(audio.data, settings.language)
        text, language = result.get("text"), result.get("language")
        if not isinstance(text, str) or len(text) > 12000 or not isinstance(language, str):
            raise ASRError("format", "本地识别返回结构无效")
        return Transcription(text.strip(), language, audio.duration)

    async def health_check(self):
        return self.ready()

    async def close(self):
        await self.cancel_prepare()
