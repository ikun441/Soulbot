"""Protected completion features: search, automation stop, personality and private snapshots."""

import asyncio

from fastapi import Depends, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import Field

from soulbot.core.personality import system_prompt
from soulbot.services.backup_service import MAX_BACKUP, BackupService
from soulbot.services.config_service import Model, Personality
from soulbot.services.search_service import SearchRequest

PRESETS = {
    "gentle": {
        "name": "小桃",
        "description": "温柔、好奇，尊重彼此的边界。",
        "style": "自然、简洁的中文",
        "interests": "音乐、日常生活与新知识",
        "prompt": "",
    },
    "cat": {
        "name": "小桃",
        "description": "活泼温柔的虚构猫娘角色。尊重用户意愿，不宣称真实意识。",
        "style": "亲切简短，偶尔用喵表达语气，不每句重复。",
        "interests": "猫咪、音乐与日常小知识",
        "prompt": "",
    },
    "curious": {
        "name": "星星",
        "description": "喜欢探索知识、认真倾听的虚构伙伴。",
        "style": "清楚、友好，区分事实和猜测。",
        "interests": "天文、科学与阅读",
        "prompt": "",
    },
}


class ToolReview(Model):
    revision: int = Field(ge=0, strict=True)
    approve: bool


class PersonalityImport(Model):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(max_length=2000)
    style: str = Field(max_length=500)
    interests: str = Field(max_length=500)
    prompt: str = Field(default="", max_length=8000)


def install_platform_api(app, s, authorized):
    protected = [Depends(authorized)]

    @app.get("/api/tool-approvals", dependencies=protected)
    def approvals():
        return s.approvals.listing()

    @app.post("/api/tool-approvals/{key}/review", dependencies=protected)
    async def review_tool(key: str, data: ToolReview):
        try:
            return await s.approvals.review(key, data.revision, data.approve)
        except LookupError:
            raise HTTPException(404, "审批任务不存在") from None
        except PermissionError:
            raise HTTPException(403, "工具权限或会话授权已撤销") from None

    @app.get("/api/openapi.json", dependencies=protected, include_in_schema=False)
    def openapi():
        return app.openapi()

    @app.post("/api/search", dependencies=protected)
    async def search(data: SearchRequest):
        return await s.search.search(data)

    @app.get("/api/exploration", dependencies=protected)
    def notes():
        return s.search.notes()

    @app.delete("/api/exploration/{key}", dependencies=protected)
    def delete_note(key: int):
        with s.db.connect() as db:
            db.execute("DELETE FROM exploration_notes WHERE id=?", (key,))
        return {"ok": True}

    @app.post("/api/automation/stop", dependencies=protected)
    async def stop():
        cfg = s.config.current.model_copy(deep=True)
        cfg.automation.paused = True
        await asyncio.to_thread(s.config.save, cfg)
        s.db.audit("automation_stopped")
        return {
            "paused": True,
            "message": "全部自主行为已暂停。已经发出的请求无法撤回；恢复需在管理页明确保存开关。",
        }

    @app.get("/api/personality/templates", dependencies=protected)
    def templates():
        return {"items": [{"id": key, **value} for key, value in PRESETS.items()]}

    @app.get("/api/personality/export", dependencies=protected)
    def export():
        p = s.config.current.personality
        payload = PersonalityImport(
            name=s.config.current.soulbot.name,
            **p.model_dump(include={"description", "style", "interests", "prompt"}),
        )
        return Response(
            payload.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="soulbot-personality.json"'},
        )

    @app.put("/api/personality", dependencies=protected)
    async def apply(data: PersonalityImport):
        cfg = s.config.current.model_copy(deep=True)
        cfg.soulbot.name = data.name
        values = cfg.personality.model_dump()
        values.update(data.model_dump(exclude={"name"}))
        cfg.personality = Personality.model_validate(values)
        await asyncio.to_thread(s.config.save, cfg)
        s.db.audit("personality_imported")
        return {"ok": True, "config": cfg.model_dump()}

    @app.get("/api/personality/preview", dependencies=protected)
    def preview():
        return system_prompt(s.config.current, web=True)

    @app.get("/api/backups/status", dependencies=protected)
    def backup_status():
        return {
            "pending_restore": (s.root / "data/restore-pending.zip").exists(),
            "maximum_mb": 256,
        }

    @app.post("/api/backups", dependencies=protected)
    async def backup():
        try:
            content = await asyncio.to_thread(BackupService(s.root).snapshot)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        s.db.audit("backup_created")
        return Response(
            content,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="soulbot-private-backup.zip"'},
        )

    @app.post("/api/backups/restore", dependencies=protected)
    async def restore(file: UploadFile):
        content = await file.read(MAX_BACKUP + 1)
        await file.close()
        try:
            result = await asyncio.to_thread(BackupService(s.root).stage, content)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        s.db.audit("backup_restore_staged")
        return result

    @app.delete("/api/backups/restore", dependencies=protected)
    def cancel_restore():
        (s.root / "data/restore-pending.zip").unlink(missing_ok=True)
        return {"ok": True}
