"""Administration API and composition root for the shared SoulBot services."""

import asyncio
import hashlib
import json
import platform
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from soulbot import VERSION
from soulbot.core.agent import SoulBotAgent
from soulbot.core.budget import BudgetService
from soulbot.core.consent import ConsentStore
from soulbot.core.emotion import EmotionEngine
from soulbot.core.memory import MemoryEngine
from soulbot.core.permissions import PermissionEngine
from soulbot.core.relationship import RelationshipEngine
from soulbot.core.scheduler import ProactiveScheduler
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import ProviderError
from soulbot.providers.registry import ProviderRegistry
from soulbot.services.asr_service import ASRService
from soulbot.services.audio_service import AudioStore
from soulbot.services.auth_service import AccountChange, AuthService
from soulbot.services.auth_service import AdminAccount as Account
from soulbot.services.config_service import ConfigService, Model, Settings
from soulbot.services.diary_service import DiaryService
from soulbot.services.environment_service import EnvironmentError, EnvironmentService
from soulbot.services.image_service import ImageGenerationService
from soulbot.services.llm_service import LLMService
from soulbot.services.media_service import MediaError, MediaStore
from soulbot.services.meme_service import MemeService
from soulbot.services.provider_diagnostics import ProviderDiagnostics
from soulbot.services.qzone_service import QZoneService
from soulbot.services.render_service import HTMLRenderService
from soulbot.services.secret_service import SecretService
from soulbot.services.tts_service import TTSService
from soulbot.storage.database import Database
from soulbot.tools.emoji import EmojiLibrary
from soulbot.tools.media_tools import register_media_tools
from soulbot.tools.registry import ToolRegistry

STATIC = Path(__file__).parent / "web" / "static"


class Login(Model):
    username: str = Field(max_length=40)
    password: SecretStr = Field(max_length=128)


class SecretUpdate(Model):
    llm_api_key: SecretStr | None = None
    onebot_access_token: SecretStr | None = None
    llm_fallback_1: SecretStr | None = None
    llm_fallback_2: SecretStr | None = None
    tts_api_key: SecretStr | None = None
    asr_api_key: SecretStr | None = None
    image_api_key: SecretStr | None = None


class Progress(Model):
    step: int = Field(ge=0, le=7)


@dataclass
class Services:
    root: Path
    config: ConfigService
    secrets: SecretService
    db: Database
    auth: AuthService
    providers: ProviderRegistry
    tools: ToolRegistry
    started: float
    running_config: Settings
    running_token_digest: str
    qq_runtime: bool = False
    budget: BudgetService | None = None
    llm: LLMService | None = None
    agent: SoulBotAgent | None = None
    qq_transport: object | None = None
    media: MediaStore | None = None
    emoji: EmojiLibrary | None = None
    meme: MemeService | None = None
    renderer: HTMLRenderService | None = None
    audio: AudioStore | None = None
    speech: TTSService | None = None
    recognition: ASRService | None = None
    images: ImageGenerationService | None = None
    diagnostics: ProviderDiagnostics | None = None
    emotion: EmotionEngine | None = None
    relationships: RelationshipEngine | None = None
    memory: MemoryEngine | None = None
    consent: ConsentStore | None = None
    scheduler: ProactiveScheduler | None = None
    diary: DiaryService | None = None
    qzone: QZoneService | None = None


def build_services(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True, mode=0o700)
    (root / "logs").mkdir(exist_ok=True)
    environment = EnvironmentService(root)
    config = ConfigService(root, environment)
    db = Database(root / "data" / "soulbot.db")
    secrets_service = SecretService(root, environment)
    services = Services(
        root,
        config,
        secrets_service,
        db,
        AuthService(db, environment),
        ProviderRegistry(),
        ToolRegistry(PermissionEngine(), db.audit),
        time.monotonic(),
        config.current.model_copy(deep=True),
        hashlib.sha256(secrets_service.get("onebot_access_token").encode()).hexdigest(),
    )
    services.budget = BudgetService(db)
    services.emotion = EmotionEngine(config, db)
    services.relationships = RelationshipEngine(config, db)
    services.memory = MemoryEngine(config, db)
    services.consent = ConsentStore(config, db)
    services.diary = DiaryService(config, db)
    services.qzone = QZoneService(services)
    services.diagnostics = ProviderDiagnostics()
    services.llm = LLMService(
        config,
        services.secrets,
        services.providers,
        services.budget,
        db.audit,
        services.diagnostics,
    )
    services.media = MediaStore(root, db)
    services.media.cleanup()
    services.audio = AudioStore(root, db)
    services.audio.cleanup()
    services.speech = TTSService(
        config, services.secrets, services.providers, services.budget, db, services.diagnostics
    )
    services.recognition = ASRService(
        root,
        config,
        services.secrets,
        services.providers,
        services.budget,
        db,
        services.diagnostics,
    )
    services.images = ImageGenerationService(
        config, services.secrets, services.providers, services.budget, db
    )
    services.emoji = EmojiLibrary(root, config, db)
    if config.current.emoji.enabled:
        services.emoji.refresh()
    services.meme = MemeService(root, config, db.audit, db)
    services.renderer = HTMLRenderService(root, config, db.audit)
    services.providers.register("html_render", "chromium", services.renderer, default=True)
    register_media_tools(services)
    services.agent = SoulBotAgent(config, db, services.llm, services)
    services.scheduler = ProactiveScheduler(services)
    services.agent.executor.proactive_guard = services.scheduler.allowed_event
    with db.connect() as connection:
        connection.execute(
            "UPDATE llm_usage SET status='uncertain',error_kind='interrupted' WHERE status='pending'"
        )
        connection.execute(
            "UPDATE tts_usage SET status='uncertain',error_kind='interrupted' WHERE status='pending'"
        )
        connection.execute(
            "UPDATE asr_usage SET status='uncertain',error_kind='interrupted' WHERE status='pending'"
        )
        connection.execute(
            "UPDATE image_usage SET status='uncertain',error_kind='interrupted' WHERE status='pending'"
        )
    return services


def create_web_app(root: Path, services=None):
    import os

    services = services or build_services(root)
    s = services

    @asynccontextmanager
    async def lifespan(application):
        await s.meme.startup()
        await s.scheduler.startup()
        await s.diary.startup()
        await s.qzone.startup()
        try:
            yield
        finally:
            await s.qzone.close()
            await s.diary.close()
            await s.scheduler.close()
            await s.meme.close()
            await s.agent.close()
            await s.speech.close()
            await s.recognition.close()
            await s.images.close()
            await s.providers.close()

    app = FastAPI(
        title="SoulBot",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.services = s
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]", "testserver"]
    allowed_hosts += [
        h.strip() for h in os.environ.get("SOULBOT_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def protection(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            origin = request.headers.get("origin")
            if request.headers.get("x-soulbot-request") != "1" or (
                origin and urlsplit(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse(
                    {"detail": "请求来源校验失败，请刷新页面后重试"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        code = (
            422
            if exc.kind in {"not_configured", "input_limit", "unsupported_capability"}
            else 429
            if exc.kind in {"budget", "task_budget", "unknown_cost", "rate_limit"}
            else 502
        )
        return JSONResponse(
            {"detail": str(exc), "kind": exc.kind, "uncertain": exc.uncertain}, status_code=code
        )

    @app.exception_handler(MediaError)
    async def media_error(request, exc):
        result = {"detail": str(exc)}
        if kind := getattr(exc, "kind", None):
            result["kind"] = kind
        if hasattr(exc, "uncertain"):
            result["uncertain"] = bool(exc.uncertain)
        return JSONResponse(result, status_code=422)

    @app.exception_handler(StateConflict)
    async def state_conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(EnvironmentError)
    async def environment_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default response includes input, which can contain credentials.
        return JSONResponse(
            {
                "detail": "填写内容不符合要求，请检查标记的字段",
                "fields": [
                    {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors()
                ],
            },
            status_code=422,
        )

    def authorized(request: Request):
        if not s.auth.valid(request.cookies.get("soulbot_session")):
            raise HTTPException(401, "请先登录管理员账户")

    def set_session(response, token, request):
        response.set_cookie(
            "soulbot_session",
            token,
            max_age=43200,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )

    def restart_required():
        current = s.config.current
        return (
            current.web != s.running_config.web
            or current.qq != s.running_config.qq
            or hashlib.sha256(s.secrets.get("onebot_access_token").encode()).hexdigest()
            != s.running_token_digest
        )

    def configuration():
        return {
            "config": s.config.current.model_dump(),
            "secrets": s.secrets.status(),
            "restart_required": restart_required(),
            "qq_reverse_ws_url": s.config.current.qq.receiver_url(s.config.current.web),
        }

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        logged_in = s.auth.valid(request.cookies.get("soulbot_session"))
        result = {
            "version": VERSION,
            "has_admin": s.auth.has_admin(),
            "authenticated": logged_in,
            "initialized": s.db.get("initialized") == "true",
        }
        if logged_in:
            result["wizard_step"] = int(s.db.get("wizard_step") or 0)
        return result

    @app.post("/api/auth/setup")
    def setup(account: Account, request: Request, response: Response):
        s.auth.setup(account.username, account.password.get_secret_value())
        set_session(response, s.auth.session(), request)
        s.db.set("wizard_step", 2)
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(account: Login, request: Request, response: Response):
        token = s.auth.login(
            account.username, account.password.get_secret_value(), request.client.host
        )
        set_session(response, token, request)
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        s.auth.logout(request.cookies.get("soulbot_session"))
        response.delete_cookie("soulbot_session", path="/")
        return {"ok": True}

    @app.get("/api/auth/account", dependencies=[Depends(authorized)])
    def account():
        return s.auth.account()

    @app.put("/api/auth/account", dependencies=[Depends(authorized)])
    def change_account(data: AccountChange, request: Request, response: Response):
        token = s.auth.change_account(
            data.username,
            data.password.get_secret_value() if data.password is not None else None,
            data.current_password.get_secret_value(),
            request.client.host,
        )
        set_session(response, token, request)
        return {
            "ok": True,
            **s.auth.account(),
            "message": "账号已保存到 .env 并立即生效，其他登录会话已退出",
        }

    @app.get("/api/config", dependencies=[Depends(authorized)])
    def read_config():
        return configuration()

    @app.get("/api/config/schema", dependencies=[Depends(authorized)])
    def schema():
        return Settings.model_json_schema()

    @app.put("/api/config", dependencies=[Depends(authorized)])
    async def save_config(settings: Settings):
        if (
            settings.qq.enabled
            and settings.qq.requires_token(settings.web)
            and not s.secrets.get("onebot_access_token")
        ):
            raise HTTPException(422, "非本机 QQ 接入前，请先保存 Access Token")
        if settings.llm != s.config.current.llm:
            s.db.set("llm_last_test", "")
        previous = s.config.current
        await asyncio.to_thread(s.config.save, settings)
        if previous.meme != settings.meme:
            await s.meme.close()
            await s.meme.startup()
        s.db.audit("config_saved")
        return configuration()

    @app.put("/api/secrets", dependencies=[Depends(authorized)])
    def save_secrets(changes: SecretUpdate):
        values = {k: v.get_secret_value() for k, v in changes if v is not None}
        if any(len(v) > 16384 for v in values.values()):
            raise HTTPException(422, "密钥长度超出限制")
        if (
            s.config.current.qq.enabled
            and s.config.current.qq.requires_token(s.config.current.web)
            and values.get("onebot_access_token") == ""
        ):
            raise HTTPException(422, "请先停用 QQ 接入，再清除 Access Token")
        s.secrets.save(values)
        if any(name.startswith("llm_") for name in values):
            s.db.set("llm_last_test", "")
        s.db.audit("secrets_updated", ",".join(values))
        return {"secrets": s.secrets.status(), "restart_required": "onebot_access_token" in values}

    @app.get("/api/config/export", dependencies=[Depends(authorized)])
    def export_config():
        data = s.config.current.model_dump()
        data["personality"]["owner_qq"] = ""
        return Response(
            ConfigService.dump(Settings.model_validate(data)),
            media_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="soulbot-config.yaml"'},
        )

    @app.post("/api/config/restore", dependencies=[Depends(authorized)])
    async def restore_config():
        if not s.config.backup_path.exists():
            raise HTTPException(404, "还没有备份；每次保存配置会自动备份上一版")
        previous = s.config.current
        await asyncio.to_thread(s.config.restore)
        if previous.llm != s.config.current.llm:
            s.db.set("llm_last_test", "")
        if previous.meme != s.config.current.meme:
            await s.meme.close()
            await s.meme.startup()
        s.db.audit("config_restored")
        return configuration()

    @app.put("/api/wizard/progress", dependencies=[Depends(authorized)])
    def progress(data: Progress):
        s.db.set("wizard_step", data.step)
        return {"ok": True}

    @app.post("/api/wizard/complete", dependencies=[Depends(authorized)])
    def complete():
        s.db.set("initialized", "true")
        s.db.set("wizard_step", 7)
        s.db.audit("wizard_completed")
        return {"ok": True, "message": "基础配置已完成；可稍后在接入设置中继续配置 AI 和 QQ"}

    @app.get("/api/health", dependencies=[Depends(authorized)])
    def health():
        with s.db.connect() as db:
            sqlite_ok = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        return {
            "version": VERSION,
            "python": platform.python_version(),
            "sqlite": sqlite_ok,
            "uptime_seconds": int(time.monotonic() - s.started),
            "nonebot": s.qq_runtime,
            "qq_enabled": s.running_config.qq.enabled,
            "qq_connected": bool(get_bots()) if s.qq_runtime else False,
            "llm": "已配置，调用状态请运行连接测试"
            if s.llm.configured()
            else "尚未配置模型或 API Key",
            "llm_configured": s.llm.configured(),
            "llm_provider_test": s.llm.test_status(),
            "pending_conversations": len(s.agent.pending),
            "usage": s.budget.summary(s.config.current),
            "meme_generator": s.meme.status(),
            "restart_required": restart_required(),
            "stage": "M5 · 空间草稿与自主发布",
            "emotion": s.emotion.snapshot(),
        }

    @app.get("/api/providers/status", dependencies=[Depends(authorized)])
    def providers_status():
        return {
            "llm": s.llm.test_status(),
            "tts": s.speech.test_status(),
            "asr": s.recognition.test_status(),
            "image_generation": s.images.diagnostics(),
        }

    def get_bots():
        import nonebot

        return nonebot.get_bots()

    @app.post("/api/qq/test", dependencies=[Depends(authorized)])
    async def test_qq():
        # Read-only protocol probe. A WS handshake alone is not evidence of OneBot.
        import websockets
        from websockets.exceptions import WebSocketException

        if s.qq_transport and s.qq_transport.connected():
            return await s.qq_transport.status()
        if s.config.current.qq.connection == "reverse":
            return {
                "ok": False,
                "message": "尚未收到 NapCat 反向连接。请在 NapCat 的 WS 客户端启用下方接收地址；保存监听端口或 QQ 设置后需重启 SoulBot。",
                "receiver_url": s.config.current.qq.receiver_url(s.config.current.web),
                "restart_required": restart_required(),
            }
        target = s.config.current.qq.test_url
        if urlsplit(target).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(
                422, "独立协议探测仅支持本机地址；远程 NapCat 可通过已连接的适配器检测"
            )
        nonce = secrets.token_hex(8)
        token = s.secrets.get("onebot_access_token")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with asyncio.timeout(5):
                async with websockets.connect(
                    target, additional_headers=headers, max_size=1048576, open_timeout=3, proxy=None
                ) as ws:
                    await ws.send(json.dumps({"action": "get_status", "params": {}, "echo": nonce}))
                    for _ in range(20):
                        result = json.loads(await ws.recv())
                        if result.get("echo") == nonce:
                            ok = result.get("status") == "ok" and result.get("retcode") == 0
                            return {
                                "ok": ok,
                                "message": "OneBot get_status 已成功响应；是否已登录 QQ 请检查 NapCat 状态"
                                if ok
                                else "OneBot 返回失败状态，请检查 NapCat 登录与 Token",
                            }
        except (OSError, TimeoutError, ValueError, WebSocketException):
            return {
                "ok": False,
                "message": "未能收到有效 OneBot 响应。请启动 NapCat 并检查端口和 Token；若此地址是反向接收端，应在 NapCat 中填写 SoulBot 的接收地址。",
            }
        return {"ok": False, "message": "连接已建立，但未收到匹配的 OneBot get_status 回执"}

    from soulbot.web.api.chat import install_chat_api

    install_chat_api(app, s, authorized)
    from soulbot.web.api.media import install_media_api

    install_media_api(app, s, authorized)
    from soulbot.web.api.speech import install_speech_api

    install_speech_api(app, s, authorized)
    from soulbot.web.api.asr import install_asr_api

    install_asr_api(app, s, authorized)
    from soulbot.web.api.image import install_image_api

    install_image_api(app, s, authorized)
    from soulbot.web.api.social import install_social_api

    install_social_api(app, s, authorized)
    from soulbot.web.api.memory import install_memory_api

    install_memory_api(app, s, authorized)
    from soulbot.web.api.proactive import install_proactive_api

    install_proactive_api(app, s, authorized)
    from soulbot.web.api.diary import install_diary_api

    install_diary_api(app, s, authorized)
    from soulbot.web.api.qzone import install_qzone_api

    install_qzone_api(app, s, authorized)

    @app.get("/api/audit", dependencies=[Depends(authorized)])
    def audit():
        with s.db.connect() as db:
            return {
                "items": [
                    dict(row) for row in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 80")
                ]
            }

    @app.post("/api/wallpaper", dependencies=[Depends(authorized)])
    async def upload_wallpaper(file: UploadFile):
        limit = 32 * 1024 * 1024
        data = await file.read(limit + 1)
        await file.close()
        if not data or len(data) > limit:
            raise HTTPException(422, "壁纸不能为空，且不能超过 32MB")
        suffix = Path(file.filename or "").suffix.lower().lstrip(".")
        if suffix == "jpeg":
            suffix = "jpg"
        if suffix in {"png", "jpg", "gif", "webp"}:
            try:
                with Image.open(BytesIO(data)) as image:
                    if image.width * image.height > 16000000:
                        raise HTTPException(422, "壁纸分辨率过大，请缩小到 1600 万像素以内")
                    expected = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}.get(
                        image.format
                    )
                    if expected != suffix:
                        raise HTTPException(422, "图片内容与扩展名不一致")
                    image.verify()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                raise HTTPException(422, "图片损坏或不是受支持的图片") from None
            kind = "image"
        elif (suffix == "mp4" and data[4:8] == b"ftyp") or (
            suffix == "webm" and data[:4] == b"\x1aE\xdf\xa3"
        ):
            kind = "video"
        else:
            raise HTTPException(422, "支持 PNG / JPG / GIF / WebP / MP4 / WebM，请选择正确格式")
        name = f"{secrets.token_hex(16)}.{suffix}"
        directory = root / "data" / "wallpapers"
        directory.mkdir(exist_ok=True)
        (directory / name).write_bytes(data)
        s.db.audit("wallpaper_uploaded")
        return {"url": f"/api/wallpaper/{name}", "kind": kind}

    @app.get("/api/wallpaper/{name}", dependencies=[Depends(authorized)])
    def wallpaper(name: str):
        if not re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|gif|webp|mp4|webm)", name):
            raise HTTPException(404, "找不到壁纸")
        path = root / "data" / "wallpapers" / name
        if not path.is_file():
            raise HTTPException(404, "壁纸文件已不存在，请重新上传")
        return FileResponse(path)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
