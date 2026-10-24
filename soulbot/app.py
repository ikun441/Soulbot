"""Administration app. No QQ event handlers or model calls are enabled in M0."""

import asyncio
import hashlib
import json
import platform
import re
import secrets
import time
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
from soulbot.core.permissions import PermissionEngine
from soulbot.providers.registry import ProviderRegistry
from soulbot.services.auth_service import AuthService
from soulbot.services.config_service import ConfigService, Model, Settings
from soulbot.services.secret_service import SecretService
from soulbot.storage.database import Database
from soulbot.tools.registry import ToolRegistry

STATIC = Path(__file__).parent / "web" / "static"


class Account(Model):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_\-]+$")
    password: SecretStr = Field(min_length=12, max_length=128)


class Login(Model):
    username: str = Field(max_length=40)
    password: SecretStr = Field(max_length=128)


class SecretUpdate(Model):
    llm_api_key: SecretStr | None = None
    onebot_access_token: SecretStr | None = None


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


def build_services(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True, mode=0o700)
    (root / "logs").mkdir(exist_ok=True)
    config = ConfigService(root)
    db = Database(root / "data" / "soulbot.db")
    return Services(
        root,
        config,
        SecretService(root),
        db,
        AuthService(db),
        ProviderRegistry(),
        ToolRegistry(PermissionEngine(), db.audit),
        time.monotonic(),
        config.current.model_copy(deep=True),
        hashlib.sha256(SecretService(root).get("onebot_access_token").encode()).hexdigest(),
    )


def create_web_app(root: Path, services=None):
    import os

    services = services or build_services(root)
    s = services
    app = FastAPI(title="SoulBot", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
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

    @app.get("/api/config", dependencies=[Depends(authorized)])
    def read_config():
        return configuration()

    @app.get("/api/config/schema", dependencies=[Depends(authorized)])
    def schema():
        return Settings.model_json_schema()

    @app.put("/api/config", dependencies=[Depends(authorized)])
    def save_config(settings: Settings):
        if settings.qq.enabled and not s.secrets.get("onebot_access_token"):
            raise HTTPException(422, "启用 QQ 接入前，请先保存 Access Token")
        s.config.save(settings)
        s.db.audit("config_saved")
        return configuration()

    @app.put("/api/secrets", dependencies=[Depends(authorized)])
    def save_secrets(changes: SecretUpdate):
        values = {k: v.get_secret_value() for k, v in changes if v is not None}
        if any(len(v) > 16384 for v in values.values()):
            raise HTTPException(422, "密钥长度超出限制")
        if s.config.current.qq.enabled and values.get("onebot_access_token") == "":
            raise HTTPException(422, "请先停用 QQ 接入，再清除 Access Token")
        s.secrets.save(values)
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
    def restore_config():
        if not s.config.backup_path.exists():
            raise HTTPException(404, "还没有备份；每次保存配置会自动备份上一版")
        s.config.restore()
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
            "llm": "尚未实现，M1 开放调用",
            "meme_generator": "尚未安装，M2 集成",
            "restart_required": restart_required(),
            "stage": "M0 · 基础架构",
        }

    def get_bots():
        import nonebot

        return nonebot.get_bots()

    @app.post("/api/qq/test", dependencies=[Depends(authorized)])
    async def test_qq():
        # Read-only protocol probe. A WS handshake alone is not evidence of OneBot.
        import websockets

        target = s.config.current.qq.test_url
        if urlsplit(target).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(422, "M0 测试仅支持本机地址；远程接入请等待 M1")
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
                                "message": "OneBot get_status 已成功响应（仅协议检测，QQ消息尚未联调）"
                                if ok
                                else "OneBot 返回失败状态，请检查 NapCat 登录与 Token",
                            }
        except (OSError, TimeoutError, ValueError, websockets.exceptions.WebSocketException):
            return {
                "ok": False,
                "message": "未能收到有效 OneBot 响应。请启动 NapCat 并检查端口和 Token；若此地址是反向接收端，应在 NapCat 中填写 SoulBot 的接收地址。",
            }
        return {"ok": False, "message": "连接已建立，但未收到匹配的 OneBot get_status 回执"}

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
