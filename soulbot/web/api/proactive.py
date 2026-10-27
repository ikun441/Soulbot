"""Protected contact restrictions and administrator-reviewed private-message drafts."""

from typing import Literal

from fastapi import Depends, HTTPException, Query

from soulbot.core.consent import ContactEdit
from soulbot.core.scheduler import JobCreate, JobReview
from soulbot.core.social_state import StateConflict


def install_proactive_api(app, s, authorized):
    protected = [Depends(authorized)]

    def change(operation):
        try:
            return operation()
        except StateConflict:
            raise
        except LookupError:
            raise HTTPException(404, "任务或授权记录不存在") from None
        except ValueError:
            raise HTTPException(
                422, "请检查用户是否已从 QQ 私聊授权、时间窗口及未结束任务数量"
            ) from None

    @app.get("/api/proactive/contacts", dependencies=protected)
    def contacts(
        query: str = Query(default="", max_length=40),
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=30, ge=1, le=100),
    ):
        return s.consent.listing(query, offset, limit)

    @app.put("/api/proactive/contacts/{key}", dependencies=protected)
    def edit_contact(key: int, data: ContactEdit):
        return change(lambda: s.consent.edit(key, data))

    @app.get("/api/proactive/jobs", dependencies=protected)
    def jobs(
        status: Literal[
            "all",
            "pending_approval",
            "scheduled",
            "waiting",
            "running",
            "sent",
            "uncertain",
            "cancelled",
            "rejected",
            "expired",
        ] = "all",
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=30, ge=1, le=100),
    ):
        return s.scheduler.listing(status, offset, limit)

    @app.post("/api/proactive/jobs", dependencies=protected)
    def create(data: JobCreate):
        return change(lambda: s.scheduler.create(data))

    @app.post("/api/proactive/jobs/{key}/review", dependencies=protected)
    def review(key: str, data: JobReview):
        return change(lambda: s.scheduler.review(key, data))
