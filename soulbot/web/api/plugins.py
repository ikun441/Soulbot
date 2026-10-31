import asyncio
import json
from typing import Any, Literal

from fastapi import Depends
from pydantic import Field, field_validator

from soulbot.services.config_service import Model
from soulbot.services.media_service import MediaError
from soulbot.services.plugin_manifest import PLUGIN_ID


class PluginEdit(Model):
    revision: int = Field(ge=0, strict=True)
    values: dict[str, Any] = Field(default_factory=dict, max_length=50)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=50)
    clear_secrets: list[str] = Field(default_factory=list, max_length=50)
    policy: Literal["deny", "auto", "approval"] = "approval"

    @field_validator("values", "secrets")
    @classmethod
    def bounded(cls, value):
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 65536:
            raise ValueError("插件参数过大")
        return value


class PluginToggle(Model):
    revision: int = Field(ge=0, strict=True)
    acknowledge_python_execution: Literal[True]


class PluginRevision(Model):
    revision: int = Field(ge=0, strict=True)


class PluginInstall(Model):
    id: str = Field(pattern=PLUGIN_ID)
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    acknowledge_download: Literal[True]


def install_plugins_api(app, s, authorized):
    protected = [Depends(authorized)]

    @app.get("/api/plugins", dependencies=protected)
    async def plugins():
        value = await asyncio.to_thread(s.plugins.listing)
        value["catalog"] = s.plugins.catalog()
        value["install"] = json.loads(s.db.get("plugin_install") or "{}")
        value["installing"] = bool(s.plugins.install_task and not s.plugins.install_task.done())
        return value

    @app.post("/api/plugins/market/refresh", dependencies=protected)
    async def refresh():
        try:
            return await s.plugins.refresh()
        except (ValueError, KeyError, TypeError):
            raise MediaError("插件索引格式不符合规范") from None

    @app.post("/api/plugins/install", dependencies=protected)
    async def install(data: PluginInstall):
        return s.plugins.begin_install(data.id, data.commit)

    @app.put("/api/plugins/{plugin_id}/config", dependencies=protected)
    async def configure(plugin_id: str, data: PluginEdit):
        try:
            return await asyncio.to_thread(
                s.plugins.configure,
                plugin_id,
                data.revision,
                data.values,
                data.secrets,
                data.clear_secrets,
                data.policy,
            )
        except (ValueError, KeyError, TypeError) as error:
            from soulbot.core.social_state import StateConflict

            if isinstance(error, StateConflict):
                raise
            raise MediaError("插件参数不符合声明、边界或密钥规则") from None

    @app.post("/api/plugins/{plugin_id}/enable", dependencies=protected)
    async def enable(plugin_id: str, data: PluginToggle):
        return await s.plugins.enable(plugin_id, data.revision)

    @app.post("/api/plugins/{plugin_id}/disable", dependencies=protected)
    async def disable(plugin_id: str, data: PluginRevision):
        return await s.plugins.disable(plugin_id, data.revision)
