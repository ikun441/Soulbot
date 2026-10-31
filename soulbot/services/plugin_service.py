"""Explicitly enabled folder plugins and pinned, hash-verified market downloads."""

import asyncio
import hashlib
import importlib.util
import inspect
import json
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from soulbot.core.social_state import StateConflict
from soulbot.services.config_service import atomic_write
from soulbot.services.media_service import MediaError
from soulbot.services.plugin_manifest import (
    MAX_FILE,
    MAX_PACKAGE,
    PLUGIN_ID,
    Catalog,
    CatalogEntry,
    Manifest,
    relative_file,
)
from soulbot.tools.registry import Tool


class PluginAPI:
    def __init__(self, manager, plugin_id):
        self.manager, self.id = manager, plugin_id
        self.tools = []

    @property
    def params(self):
        return self.manager.parameters(self.id)

    def tool(
        self, *, name, description, schema, handler, permission="approval", timeout=10, max_calls=2
    ):
        full = f"plugin_{self.id}__{name}"
        if (
            not re.fullmatch(PLUGIN_ID, name)
            or len(full) > 64
            or not isinstance(description, str)
            or not 1 <= len(description) <= 500
            or not isinstance(schema, type)
            or not issubclass(schema, BaseModel)
            or not inspect.iscoroutinefunction(handler)
            or permission not in {"deny", "auto", "approval"}
            or type(timeout) not in {int, float}
            or not 1 <= timeout <= 120
            or type(max_calls) is not int
            or not 1 <= max_calls <= 5
            or len(self.tools) >= 10
            or any(t[0].name == full for t in self.tools)
        ):
            raise ValueError("插件工具声明无效")
        checked = type(
            "PluginArgs_" + name, (schema,), {"model_config": ConfigDict(extra="forbid")}
        )

        async def guarded(args, context):
            if not await asyncio.to_thread(self.manager.allowed, self.id):
                raise PermissionError("插件已关闭或代码已变化")
            task = asyncio.current_task()
            self.manager.active.setdefault(self.id, set()).add(task)
            try:
                result = await handler(args, context)
                if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 65536:
                    raise ValueError("插件返回结果过大")
                if not self.manager.state(self.id)["enabled"]:
                    raise PermissionError("插件已关闭")
                return result
            finally:
                self.manager.active[self.id].discard(task)

        self.tools.append(
            (
                Tool(
                    full,
                    description,
                    checked,
                    guarded,
                    timeout=timeout,
                    max_calls=max_calls,
                    with_context=True,
                ),
                name,
                permission,
            )
        )


class PluginService:
    def __init__(self, services):
        self.s = services
        self.root = services.root / "plugins"
        if not self.root.exists():
            self.root.mkdir(parents=True)
        self.secret_path = services.root / "data/plugin-secrets.json"
        self.secret_lock = threading.RLock()
        self.runtime = {}
        self.active = {}
        self.load_lock = asyncio.Lock()
        self.install_task = None
        self.transport = None

    def folder(self, plugin_id):
        if not re.fullmatch(PLUGIN_ID, plugin_id):
            raise MediaError("插件ID无效")
        if self.root.is_symlink() or not self.root.is_dir():
            raise MediaError("插件目录必须是本机普通目录")
        folder = self.root / plugin_id
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.root.resolve()):
            raise MediaError("插件目录不能使用符号链接")
        return folder

    def manifest(self, plugin_id):
        folder = self.folder(plugin_id)
        path = folder / "config.yaml"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise MediaError("插件缺少有效的 config.yaml")
        try:
            return Manifest.parse(path.read_bytes())
        except Exception:
            raise MediaError("插件配置格式或参数声明无效") from None

    def digest(self, plugin_id):
        folder = self.folder(plugin_id)
        files = {}
        total = 0
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if "__pycache__" in relative.parts or path.name == ".DS_Store":
                continue
            if path.is_symlink():
                raise MediaError("插件包包含符号链接")
            if not path.is_file():
                continue
            name = relative_file(relative.as_posix())
            size = path.stat().st_size
            if size > MAX_FILE or total + size > MAX_PACKAGE or len(files) >= 100:
                raise MediaError("插件包超过文件或大小限制")
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            total += size
        if not {"plugin.py", "config.yaml"} <= files.keys():
            raise MediaError("插件缺少入口文件")
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def state(self, plugin_id):
        with self.s.db.connect() as db:
            row = db.execute("SELECT * FROM plugin_settings WHERE id=?", (plugin_id,)).fetchone()
        return (
            dict(row)
            if row
            else {
                "id": plugin_id,
                "enabled": 0,
                "values_json": "{}",
                "policy": "approval",
                "approved_hash": "",
                "revision": 0,
            }
        )

    def secret_values(self):
        if self.secret_path.is_symlink():
            raise MediaError("插件密钥存储路径无效")
        if not self.secret_path.exists():
            return {}
        if self.secret_path.stat().st_size > 1024 * 1024:
            raise MediaError("插件密钥存储过大")
        value = json.loads(self.secret_path.read_text())
        if not isinstance(value, dict):
            raise MediaError("插件密钥存储格式无效")
        return value

    def parameters(self, plugin_id, public=False):
        manifest = self.manifest(plugin_id)
        values = json.loads(self.state(plugin_id)["values_json"])
        if not values.keys() <= manifest.parameters.keys():
            raise MediaError("参数与当前插件版本不兼容，请重新配置")
        secrets = self.secret_values().get(plugin_id, {})
        result = {}
        for name, field in manifest.parameters.items():
            if field.secret:
                if not public:
                    result[name] = field.checked(secrets.get(name, ""))
            else:
                result[name] = field.checked(
                    values.get(name, manifest.config.get(name, field.default))
                )
        return result

    def listing(self):
        items = []
        errors = []
        if self.root.is_symlink() or not self.root.is_dir():
            return {"items": [], "errors": ["插件目录无效"]}
        for folder in sorted(self.root.iterdir()):
            if folder.name.startswith(".") or not folder.is_dir():
                continue
            if len(items) + len(errors) >= 200:
                break
            try:
                manifest = self.manifest(folder.name)
                state = self.state(folder.name)
                digest = self.digest(folder.name)
                values = self.parameters(folder.name, True)
                secrets = self.secret_values().get(folder.name, {})
                items.append(
                    {
                        "id": folder.name,
                        "define": manifest.define.model_dump(),
                        "parameters": {k: v.model_dump() for k, v in manifest.parameters.items()},
                        "config": values,
                        "secrets": {
                            k: bool(secrets.get(k))
                            for k, v in manifest.parameters.items()
                            if v.secret
                        },
                        "enabled": bool(state["enabled"] and folder.name in self.runtime),
                        "changed": bool(
                            state["approved_hash"] and state["approved_hash"] != digest
                        ),
                        "policy": state["policy"],
                        "revision": state["revision"],
                        "tools": [t.name for t, _, _ in self.runtime.get(folder.name, [])],
                    }
                )
            except Exception:
                errors.append(
                    {"id": folder.name, "message": "插件元数据、参数、路径或包结构无效，未执行代码"}
                )
        return {"items": items, "errors": errors}

    def configure(self, plugin_id, revision, values, secret_changes, clear_secrets, policy):
        manifest = self.manifest(plugin_id)
        if policy not in {"deny", "auto", "approval"}:
            raise MediaError("插件权限无效")
        if (
            not values.keys() <= manifest.parameters.keys()
            or not secret_changes.keys() <= manifest.parameters.keys()
        ):
            raise MediaError("插件参数未声明")
        checked = {}
        for name, value in values.items():
            field = manifest.parameters[name]
            if field.secret:
                raise MediaError("密钥须通过独立字段保存")
            checked[name] = field.checked(value)
        for name, value in secret_changes.items():
            field = manifest.parameters[name]
            if not field.secret:
                raise MediaError("此参数不是密钥")
            field.checked(value)
        if set(clear_secrets) & {k for k, v in secret_changes.items() if v}:
            raise MediaError("不能同时保存和清除同一密钥")
        if any(
            name not in manifest.parameters or not manifest.parameters[name].secret
            for name in clear_secrets
        ):
            raise MediaError("密钥字段无效")
        with self.secret_lock, self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT * FROM plugin_settings WHERE id=?", (plugin_id,)).fetchone()
            if (state["revision"] if state else 0) != revision:
                raise StateConflict("插件设置已变化，请刷新")
            previous = json.loads(state["values_json"]) if state else {}
            # Drop removed fields only during an explicit administrator save.
            previous = {
                k: v
                for k, v in previous.items()
                if k in manifest.parameters and not manifest.parameters[k].secret
            }
            previous.update(checked)
            for name, value in previous.items():
                manifest.parameters[name].checked(value)
            secrets = self.secret_values()
            own = secrets.setdefault(plugin_id, {})
            own.update({k: v for k, v in secret_changes.items() if v})
            for name in clear_secrets:
                own.pop(name, None)
            atomic_write(self.secret_path, json.dumps(secrets, ensure_ascii=False))
            db.execute(
                "INSERT INTO plugin_settings(id,enabled,values_json,policy,approved_hash,revision) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET values_json=excluded.values_json,policy=excluded.policy,revision=excluded.revision",
                (
                    plugin_id,
                    state["enabled"] if state else 0,
                    json.dumps(previous, ensure_ascii=False),
                    policy,
                    state["approved_hash"] if state else "",
                    revision + 1,
                ),
            )
        self.apply_policy(plugin_id)
        self.s.db.audit("plugin_configured", plugin_id)
        return {"ok": True, "revision": revision + 1}

    def apply_policy(self, plugin_id):
        state = self.state(plugin_id)
        for tool, _, declared in self.runtime.get(plugin_id, []):
            self.s.tools.permissions.dynamic[tool.name] = (
                "deny" if declared == "deny" else state["policy"]
            )

    def allowed(self, plugin_id):
        if not self.s.config.current.plugins.enabled or plugin_id not in self.runtime:
            return False
        state = self.state(plugin_id)
        return bool(state["enabled"] and state["approved_hash"] == self.digest(plugin_id))

    async def enable(self, plugin_id, revision):
        async with self.load_lock:
            if not self.s.config.current.plugins.enabled:
                raise MediaError("插件总开关已关闭")
            if plugin_id in self.runtime:
                return {"ok": True, "enabled": True}
            self.manifest(plugin_id)
            digest = await asyncio.to_thread(self.digest, plugin_id)
            self.parameters(plugin_id)
            state = self.state(plugin_id)
            if state["revision"] != revision:
                raise StateConflict("插件设置已变化，请刷新")
            api = PluginAPI(self, plugin_id)
            namespace = f"soulbot_plugin_{plugin_id}_{digest[:12]}"

            def load():
                spec = importlib.util.spec_from_file_location(
                    namespace,
                    self.folder(plugin_id) / "plugin.py",
                    submodule_search_locations=[str(self.folder(plugin_id))],
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[namespace] = module
                spec.loader.exec_module(module)
                if not callable(getattr(module, "register", None)):
                    raise ValueError("缺少 register")
                value = module.register(api)
                if inspect.isawaitable(value):
                    raise ValueError("register 必须是同步函数")
                if not api.tools:
                    raise ValueError("插件没有注册工具")

            try:
                await asyncio.wait_for(asyncio.to_thread(load), 10)
                if digest != await asyncio.to_thread(self.digest, plugin_id):
                    raise StateConflict("插件代码已变化，请重新核对")
                if any(t.name in self.s.tools.tools for t, _, _ in api.tools):
                    raise MediaError("插件工具命名冲突")
                with self.s.db.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT revision FROM plugin_settings WHERE id=?", (plugin_id,)
                    ).fetchone()
                    if (row[0] if row else 0) != revision:
                        raise StateConflict("插件设置已变化，请刷新")
                    db.execute(
                        "INSERT INTO plugin_settings(id,enabled,values_json,policy,approved_hash,revision) VALUES (?,1,'{}','approval',?,?) ON CONFLICT(id) DO UPDATE SET enabled=1,approved_hash=excluded.approved_hash,revision=excluded.revision",
                        (plugin_id, digest, revision + 1),
                    )
                self.runtime[plugin_id] = api.tools
                for tool, _, _ in api.tools:
                    self.s.tools.register(tool)
                self.apply_policy(plugin_id)
                self.s.db.audit("plugin_enabled", plugin_id)
                return {"ok": True, "enabled": True, "revision": revision + 1}
            except (StateConflict, MediaError):
                raise
            except Exception:
                sys.modules.pop(namespace, None)
                self.s.db.audit("plugin_load_failed", plugin_id)
                raise MediaError("插件加载未完成，请检查入口、依赖和工具声明；未启用") from None

    async def disable(self, plugin_id, revision=None):
        self.folder(plugin_id)
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision FROM plugin_settings WHERE id=?", (plugin_id,)
            ).fetchone()
            if revision is not None and (row[0] if row else 0) != revision:
                raise StateConflict("插件状态已变化，请刷新")
            db.execute(
                "UPDATE plugin_settings SET enabled=0,revision=revision+1 WHERE id=?", (plugin_id,)
            )
        for tool, _, _ in self.runtime.pop(plugin_id, []):
            self.s.tools.tools.pop(tool.name, None)
            self.s.tools.permissions.dynamic.pop(tool.name, None)
        jobs = list(self.active.get(plugin_id, set()))
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.s.db.audit("plugin_disabled", plugin_id)
        return {"ok": True, "enabled": False}

    async def fetch(self, url, maximum=MAX_FILE):
        token = self.s.secrets.get("plugin_github_token")
        headers = (
            {"Accept": "application/vnd.github+json"}
            if url.startswith("https://api.github.com/")
            else {}
        )
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=20
            ) as c:
                async with c.stream("GET", url, headers=headers) as r:
                    if r.status_code != 200:
                        raise MediaError("无法读取插件仓库，请检查仓库、分支、令牌权限或网络")
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > maximum:
                            raise MediaError("插件下载超过大小限制")
                    return bytes(data)
        except httpx.HTTPError:
            raise MediaError("插件仓库网络请求失败，请稍后重试") from None

    async def refresh(self):
        cfg = self.s.config.current.plugins
        commit = json.loads(
            await self.fetch(
                f"https://api.github.com/repos/{cfg.repository}/commits/{quote(cfg.branch, safe='')}"
            )
        )["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise MediaError("插件仓库未返回有效提交")
        catalog = Catalog.model_validate_json(
            await self.fetch(
                f"https://raw.githubusercontent.com/{cfg.repository}/{commit}/index.json",
                1024 * 1024,
            )
        )
        value = {
            "repository": cfg.repository,
            "commit": commit,
            "items": [p.model_dump() for p in catalog.plugins],
        }
        self.s.db.set("plugin_catalog", json.dumps(value, ensure_ascii=False))
        self.s.db.audit("plugin_catalog_refreshed")
        return value

    def catalog(self):
        return json.loads(self.s.db.get("plugin_catalog") or '{"items":[]}')

    async def install(self, plugin_id, commit):
        state = self.catalog()
        repo = self.s.config.current.plugins.repository
        if state.get("repository") != repo or state.get("commit") != commit:
            raise MediaError("市场索引已变化，请刷新后重试")
        item = next(
            (CatalogEntry.model_validate(v) for v in state["items"] if v["id"] == plugin_id), None
        )
        if item is None:
            raise MediaError("插件不在已核对的市场索引中")
        destination = self.folder(plugin_id)
        if destination.exists():
            raise MediaError("插件已安装；不会覆盖现有目录或设置")
        self.s.db.set("plugin_install", json.dumps({"id": plugin_id, "status": "downloading"}))
        try:
            with tempfile.TemporaryDirectory(dir=self.root, prefix=".install-") as tmp:
                package = Path(tmp) / plugin_id
                package.mkdir()
                total = 0
                for name, digest in item.files.items():
                    content = await self.fetch(
                        f"https://raw.githubusercontent.com/{repo}/{commit}/{item.path}/{quote(name, safe='/')}"
                    )
                    total += len(content)
                    if total > MAX_PACKAGE or hashlib.sha256(content).hexdigest() != digest:
                        raise MediaError("插件下载校验失败，未安装")
                    path = package / relative_file(name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    path.chmod(0o600)
                manifest = Manifest.parse((package / "config.yaml").read_bytes())
                if manifest.define.model_dump() != {
                    k: getattr(item, k) for k in ("name", "author", "version", "info")
                }:
                    raise MediaError("插件说明与市场索引不一致")
                if repo != self.s.config.current.plugins.repository:
                    raise MediaError("插件源已变化，停止安装")
                if destination.exists():
                    raise MediaError("插件目录已存在，未覆盖")
                shutil.move(str(package), str(destination))
            self.s.db.set("plugin_install", json.dumps({"id": plugin_id, "status": "installed"}))
            self.s.db.audit("plugin_installed", plugin_id)
        except asyncio.CancelledError:
            self.s.db.set("plugin_install", json.dumps({"id": plugin_id, "status": "cancelled"}))
            raise
        except Exception:
            self.s.db.set("plugin_install", json.dumps({"id": plugin_id, "status": "failed"}))
            self.s.db.audit("plugin_install_failed", plugin_id)

    def begin_install(self, plugin_id, commit):
        state = self.catalog()
        if (
            state.get("commit") != commit
            or state.get("repository") != self.s.config.current.plugins.repository
            or not any(v.get("id") == plugin_id for v in state.get("items", []))
        ):
            raise MediaError("插件不在当前固定提交的索引中，请刷新市场")
        if self.folder(plugin_id).exists():
            raise MediaError("插件已安装，不覆盖已有目录")
        if self.install_task and not self.install_task.done():
            raise StateConflict("已有插件正在安装")
        self.install_task = asyncio.create_task(
            self.install(plugin_id, commit), name="soulbot-plugin-install"
        )
        return {"accepted": True, "message": "开始校验并安装插件；安装完成后仍为禁用状态。"}

    async def startup(self):
        if not self.s.config.current.plugins.enabled:
            return
        with self.s.db.connect() as db:
            rows = db.execute(
                "SELECT id,revision,approved_hash FROM plugin_settings WHERE enabled=1"
            ).fetchall()
        for row in rows:
            try:
                if row["approved_hash"] != self.digest(row["id"]):
                    raise ValueError("changed")
                await self.enable(row["id"], row["revision"])
            except Exception:
                await self.disable(row["id"])

    async def close(self):
        if self.install_task and not self.install_task.done():
            self.install_task.cancel()
            await asyncio.gather(self.install_task, return_exceptions=True)
        # Shutdown removes runtime tools, while retaining the administrator's enabled setting.
        for plugin_id, tools in list(self.runtime.items()):
            for task in self.active.get(plugin_id, set()):
                task.cancel()
            await asyncio.gather(*self.active.get(plugin_id, set()), return_exceptions=True)
            for tool, _, _ in tools:
                self.s.tools.tools.pop(tool.name, None)
                self.s.tools.permissions.dynamic.pop(tool.name, None)
        self.runtime.clear()
