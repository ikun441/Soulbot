"""Pinned upstream API client and project-local child process ownership."""

import asyncio
import json
import os
import re
import signal
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from soulbot.services.config_service import atomic_write
from soulbot.services.media_service import MAX_IMAGE_BYTES, MediaError, inspect_image
from soulbot.services.meme_extensions import MemeExtensions
from soulbot.services.meme_policy import MemeRateLimiter

VERSION = "0.1.14"
PROJECT = Path(__file__).resolve().parents[2]
KEY = re.compile(r"[a-zA-Z0-9_\-]{1,100}")


class InstanceLock:
    """OS lock is released by the OS on crashes; never unlink a live lock inode."""

    def __init__(self, path):
        self.path, self.stream = path, None

    def acquire(self):
        if self.stream:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None


class MemeClient:
    def __init__(self, config, root=None, db=None):
        self.config = config
        self.root = root
        self.rate = MemeRateLimiter(config, db)
        self.transport = None
        self.limiter = asyncio.Semaphore(2)

    def origins(self):
        if self.root and self.config.current.meme.managed:
            try:
                value = json.loads((self.root / "loaded-templates.json").read_text())["origins"]
                return value if isinstance(value, dict) else {}
            except (OSError, ValueError, KeyError):
                pass
        return {}

    def allowed(self, key, origins=None):
        origins = self.origins() if origins is None else origins
        source = origins.get(key, "builtin" if self.config.current.meme.managed else "external")
        settings = self.config.current.meme
        if key in settings.disabled_ids:
            raise MediaError("此表情 ID 已被管理员禁用")
        profile = settings.extensions.get(source)
        if profile and (not profile.enabled or key in profile.disabled_ids):
            raise MediaError("此扩展或表情 ID 已被管理员禁用")
        return source

    async def request(self, method, path, *, maximum=2 * 1024 * 1024, **kwargs):
        settings = self.config.current.meme
        if not settings.enabled:
            raise MediaError("meme-generator 尚未启用，请先保存配置")
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=settings.timeout_seconds,
                    follow_redirects=False,
                    transport=self.transport,
                ) as client:
                    async with client.stream(
                        method, settings.base_url + path, **kwargs
                    ) as response:
                        if response.status_code != 200:
                            if response.status_code in {
                                422,
                                531,
                                532,
                                541,
                                542,
                                551,
                                552,
                                553,
                                554,
                            }:
                                raise MediaError(
                                    "模板拒绝了输入，请检查图片/文字数量、文字长度和高级参数"
                                )
                            if response.status_code in {404, 520}:
                                raise MediaError("模板或 API 不存在，请核对服务版本并刷新模板列表")
                            raise MediaError("meme-generator 生成失败，请检查素材与第三方服务状态")
                        result = bytearray()
                        async for chunk in response.aiter_bytes():
                            result.extend(chunk)
                            if len(result) > maximum:
                                raise MediaError("第三方响应超过大小限制")
                        return bytes(result)
        except (TimeoutError, httpx.TimeoutException):
            raise MediaError("meme-generator 响应超时；没有自动重试生成请求") from None
        except httpx.HTTPError:
            raise MediaError("无法连接 meme-generator，请启动服务或检查地址") from None

    async def json(self, path):
        try:
            return json.loads(await self.request("GET", path))
        except (ValueError, TypeError):
            raise MediaError("第三方 API 响应格式不兼容") from None

    async def health(self):
        version = await self.json("/meme/version")
        if version != VERSION:
            raise MediaError(f"仅验证了 meme-generator {VERSION}，当前服务版本不匹配")
        keys = await self.keys()
        return {"ok": True, "version": version, "templates": len(keys)}

    async def keys(self):
        values = await self.json("/memes/keys")
        if (
            not isinstance(values, list)
            or len(values) > 10000
            or any(not isinstance(v, str) or not KEY.fullmatch(v) for v in values)
        ):
            raise MediaError("模板列表格式不兼容")
        allowed = []
        origins = self.origins()
        for key in values:
            try:
                self.allowed(key, origins)
            except MediaError:
                continue
            allowed.append(key)
        return sorted(allowed)

    async def info(self, key):
        if not KEY.fullmatch(key):
            raise MediaError("模板名称无效")
        source = self.allowed(key)
        value = await self.json(f"/memes/{key}/info")
        try:
            params = value["params_type"]
            for name in ("min_images", "max_images", "min_texts", "max_texts"):
                if type(params[name]) is not int or not 0 <= params[name] <= 100:
                    raise ValueError
            if (
                params["min_images"] > params["max_images"]
                or params["min_texts"] > params["max_texts"]
                or value["key"] != key
            ):
                raise ValueError
            if not isinstance(params["default_texts"], list):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise MediaError("模板参数元数据无效，已停止生成") from None
        value["soulbot_source"] = source
        return value

    async def preview(self, key):
        await self.info(key)
        source = self.allowed(key)
        await asyncio.to_thread(self.rate.reserve, source, "preview")
        async with self.limiter:
            self.allowed(key)
            content = await self.request("GET", f"/memes/{key}/preview", maximum=MAX_IMAGE_BYTES)
            inspect_image(content)
            return content

    async def generate(self, key, texts, images, args=None):
        info = await self.info(key)
        params = info["params_type"]
        texts = [text for text in texts if text.strip()]
        if not params["min_images"] <= len(images) <= params["max_images"]:
            raise MediaError(f"此模板需要 {params['min_images']}–{params['max_images']} 张图片")
        if not params["min_texts"] <= len(texts) <= params["max_texts"]:
            raise MediaError(f"此模板需要 {params['min_texts']}–{params['max_texts']} 段文字")
        if args:
            from jsonschema import Draft202012Validator
            from jsonschema.exceptions import SchemaError, ValidationError

            schema = (params.get("args_type") or {}).get("args_model")
            if not isinstance(schema, dict):
                raise MediaError("此模板不接受高级参数")
            pending = [schema]
            while pending:
                part = pending.pop()
                if isinstance(part, dict):
                    if "$ref" in part and not str(part["$ref"]).startswith("#/"):
                        raise MediaError("模板包含外部参数引用，已拒绝远程 Schema 访问")
                    pending.extend(part.values())
                elif isinstance(part, list):
                    pending.extend(part)
            try:
                Draft202012Validator(schema).validate(args)
            except (ValidationError, SchemaError):
                raise MediaError("高级参数不符合模板 Schema，请检查字段和类型") from None
        files = []
        for index, image in enumerate(images):
            extension, mime, _, _ = inspect_image(image.data)
            files.append(("images", (f"input{index}.{extension}", image.data, mime)))
        # Explicit multipart also when no images are required. Never pass model URLs.
        files += [("texts", (None, text)) for text in texts]
        files.append(("args", (None, json.dumps(args or {}))))
        source = self.allowed(key)
        await asyncio.to_thread(self.rate.reserve, source, "generate")
        async with self.limiter:
            self.allowed(key)
            content = await self.request(
                "POST", f"/memes/{key}/", files=files, maximum=MAX_IMAGE_BYTES
            )
            inspect_image(content)
            return content


class MemeService:
    def __init__(self, root, config, audit, db=None):
        self.root = root / "third_party" / "meme-generator"
        self.config, self.audit = config, audit
        self.client = MemeClient(config, self.root, db)
        self.extensions = MemeExtensions(self.root, config)
        self.extension_error = None
        self.python = (
            self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        self.lock = InstanceLock(self.root / "service.lock")
        self.task = self.process = None
        self.log_task = None
        self.phase, self.message, self.restarts = (
            "not_installed",
            "可在此安装项目隔离的 meme-generator",
            0,
        )
        self.active_url = None
        if self.ready():
            self.phase, self.message = "ready", "独立环境和素材已准备，可启用服务"

    def ready(self):
        try:
            return (
                self.python.is_file()
                and json.loads((self.root / "ready.json").read_text())["version"] == VERSION
            )
        except (OSError, ValueError, KeyError):
            return False

    def status(self):
        return {
            "phase": self.phase,
            "message": self.message,
            "ready": self.ready(),
            "running": bool(
                self.process and self.process.returncode is None and self.phase == "running"
            ),
            "busy": bool(
                self.task
                and not self.task.done()
                and self.phase in {"installing", "preparing", "starting", "installing_extension"}
            ),
            "managed": self.config.current.meme.managed,
            "enabled": self.config.current.meme.enabled,
            "version": VERSION,
            "restarts": self.restarts,
            "extension_error": self.extension_error,
            "extensions": self.extensions.status(self.runtime()),
        }

    def runtime(self):
        try:
            return json.loads((self.root / "loaded-templates.json").read_text())
        except (OSError, ValueError):
            return {}

    def state(self, phase, message):
        self.phase, self.message = phase, message
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.root / "state.json",
            json.dumps({"phase": phase, "message": message}, ensure_ascii=False),
        )

    @staticmethod
    def environment():
        # Child processes never receive the bot's API keys or application secrets.
        names = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        return {k: v for k, v in os.environ.items() if k in names} | {"PYTHONUNBUFFERED": "1"}

    async def drain(self, process):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "service.log"
        with path.open("wb") as log:
            path.chmod(0o600)
            size = 0
            while data := await process.stdout.read(4096):
                if size < 1024 * 1024:
                    log.write(data[: 1024 * 1024 - size])
                    log.flush()
                    size += len(data)

    async def spawn(self, command):
        process = await asyncio.create_subprocess_exec(
            *map(str, command),
            cwd=self.root,
            env=self.environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        self.process = process
        self.log_task = asyncio.create_task(self.drain(process))
        return process

    async def terminate(self):
        process = self.process
        if process and process.returncode is None:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 5)
            except (ProcessLookupError, TimeoutError):
                if process.returncode is None:
                    try:
                        if os.name == "nt":
                            process.kill()
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
        if self.log_task:
            await asyncio.gather(self.log_task, return_exceptions=True)
        self.process = self.log_task = None

    async def command(self, args, timeout):
        process = await self.spawn(args)
        try:
            code = await asyncio.wait_for(process.wait(), timeout)
            if code:
                raise MediaError("第三方准备命令失败，请检查网络、Python 版本或本机安装日志")
        finally:
            await self.terminate()

    async def prepare(self):
        self.state("installing", "正在创建独立环境和安装固定版本依赖，可继续使用核心功能")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.python.exists():
            await self.command([sys.executable, "-m", "venv", self.root / ".venv"], 120)
        await self.command(
            [self.python, "-m", "pip", "install", "-r", PROJECT / "requirements-meme.txt"], 600
        )
        self.state("preparing", "正在下载并校验官方模板素材与中文字体，首次准备可能需要数分钟")
        await self.command(
            [self.python, PROJECT / "scripts/meme_worker.py", "prepare", "--root", self.root], 900
        )
        if not self.ready():
            raise MediaError("素材准备没有完成，已保留下载进度，可重新安装/修复")

    async def begin(self, install=False, extension=None):
        if not self.config.current.meme.managed:
            raise MediaError("当前是外部服务模式；SoulBot 不会安装、启动或停止外部进程")
        if self.task and not self.task.done():
            raise MediaError("已有安装或托管任务，请先停止当前任务")
        if not self.lock.acquire():
            raise MediaError("已有另一个 SoulBot 实例管理此服务，不能重复启动")
        self.task = asyncio.create_task(
            self.supervise(install, extension), name="meme-service-supervisor"
        )
        return self.status()

    async def install_extension(self, source):
        if source not in self.extensions.catalog:
            raise MediaError("未知扩展仓库")
        if not self.config.current.meme.managed:
            raise MediaError("下载扩展需要使用 SoulBot 托管模式")
        if not self.ready():
            raise MediaError("请先完成 meme-generator 基础组件安装")
        if (
            self.phase in {"installing", "preparing", "installing_extension"}
            and self.task
            and not self.task.done()
        ):
            raise MediaError("已有准备任务，请等待完成或先停止")
        await self.close()
        return await self.begin(extension=source)

    async def supervise(self, install, extension=None):
        try:
            if extension:
                self.extension_error = None
                self.state("installing_extension", "正在下载已验证的扩展版本")
                try:
                    await self.extensions.install(
                        extension, lambda message: self.state("installing_extension", message)
                    )
                except (MediaError, OSError) as error:
                    self.extension_error = (
                        str(error)
                        if isinstance(error, MediaError)
                        else "扩展文件无法保存，请检查目录权限"
                    )
                    await asyncio.to_thread(self.audit, "meme_extension_failed", extension)
                else:
                    await asyncio.to_thread(self.audit, "meme_extension_installed", extension)
            if install:
                await self.prepare()
            if not self.config.current.meme.enabled:
                self.state(
                    "ready" if self.ready() else "disabled", "第三方服务未启用；准备好的文件会保留"
                )
                return
            if not self.ready():
                raise MediaError("请先安装并完成模板资源准备，或开启自动安装")
            self.active_url = self.config.current.meme.base_url
            port = urlsplit(self.active_url).port
            self.restarts = 0
            while True:
                with socket.socket() as probe:
                    probe.settimeout(0.2)
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        raise MediaError("meme 端口已被其它进程占用；未接管或停止未知进程")
                self.state("starting", "正在启动并验证第三方 API")
                atomic_write(
                    self.root / "worker-settings.json",
                    json.dumps(self.extensions.worker_settings(), ensure_ascii=False),
                )
                await self.spawn(
                    [
                        self.python,
                        PROJECT / "scripts/meme_worker.py",
                        "run",
                        "--root",
                        self.root,
                        "--port",
                        port,
                        "--settings",
                        self.root / "worker-settings.json",
                    ]
                )
                healthy = False
                for _ in range(40):
                    if self.process.returncode is not None:
                        break
                    try:
                        async with asyncio.timeout(3):
                            await self.client.health()
                        healthy = True
                        break
                    except (MediaError, TimeoutError):
                        await asyncio.sleep(0.5)
                if healthy:
                    self.state("running", "已验证版本与模板 API，服务正在本机运行")
                    failures = 0
                    while self.process.returncode is None:
                        await asyncio.sleep(3)
                        current = self.config.current.meme
                        if (
                            not current.enabled
                            or not current.managed
                            or current.base_url != self.active_url
                        ):
                            self.state("stopped", "配置已改变，已停止本实例托管的服务")
                            return
                        try:
                            async with asyncio.timeout(3):
                                await self.client.health()
                            failures = 0
                        except (MediaError, TimeoutError):
                            failures += 1
                            if failures >= 3:
                                break
                await self.terminate()
                if self.restarts >= self.config.current.meme.max_restarts:
                    raise MediaError("第三方服务异常且已达自动重启上限，核心服务仍可使用")
                self.restarts += 1
                self.state("restarting", f"第三方服务异常，正在进行第 {self.restarts} 次有限重启")
                await asyncio.sleep(2**self.restarts)
        except asyncio.CancelledError:
            self.state("stopped", "托管任务已停止，已保存的环境和资源会保留")
            raise
        except (MediaError, OSError, TimeoutError) as error:
            self.state(
                "failed",
                str(error)
                if isinstance(error, MediaError)
                else "第三方任务无法继续，请检查目录权限和运行环境",
            )
            await asyncio.to_thread(self.audit, "meme_service_failed", "prepare_or_runtime")
        except Exception:
            self.state(
                "failed",
                "第三方任务出现内部错误，请查看本机 third_party/meme-generator/service.log",
            )
            await asyncio.to_thread(self.audit, "meme_service_failed", "internal")
        finally:
            await self.terminate()
            self.lock.close()

    async def startup(self):
        settings = self.config.current.meme
        if settings.enabled and settings.managed and (not self.task or self.task.done()):
            try:
                await self.begin(settings.auto_install and not self.ready())
            except MediaError as error:
                self.state("failed", str(error))
        elif not settings.managed:
            self.phase, self.message = "external", "外部模式，请运行连接检查；不会管理外部进程"

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.terminate()
        self.lock.close()
