"""Local-only diary administration. There is deliberately no publish/send endpoint."""

import json

from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response

from soulbot.core.social_state import StateConflict
from soulbot.services.diary_periods import DiaryDelivery, PolishDiary, WeeklyDiary
from soulbot.services.diary_service import DiaryEdit, GenerateDiary


def install_diary_api(app, s, authorized):
    protected = [Depends(authorized)]

    def call(operation):
        try:
            return operation()
        except StateConflict:
            raise
        except LookupError:
            raise HTTPException(404, "日记不存在") from None
        except ValueError:
            raise HTTPException(
                422, "请启用本地日记，并选择保留期内的日期；当天预览需启用当天选项"
            ) from None

    @app.get("/api/diaries/status", dependencies=protected)
    def status():
        return s.diary.status()

    @app.get("/api/diaries", dependencies=protected)
    def listing(
        query: str = Query(default="", max_length=80),
        day: str = Query(default="", pattern=r"^(|\d{4}-\d{2}-\d{2})$"),
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=30, ge=1, le=100),
    ):
        return s.diary.listing(query, day, offset, limit)

    @app.post("/api/diaries/generate", dependencies=protected)
    def generate(data: GenerateDiary):
        return call(
            lambda: s.diary.generate(data.day, include_current_day=data.include_current_day)
        )

    @app.get("/api/diaries/weekly", dependencies=protected)
    def summaries():
        return s.periods.summaries()

    @app.post("/api/diaries/weekly", dependencies=protected)
    def weekly(data: WeeklyDiary):
        return call(lambda: s.periods.generate_week(data.day))

    @app.delete("/api/diaries/weekly/{key}", dependencies=protected)
    def remove_weekly(key: int):
        with s.db.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO diary_summary_deleted SELECT scope,week_start FROM diary_summaries WHERE id=?",
                (key,),
            )
            db.execute("DELETE FROM diary_summaries WHERE id=?", (key,))
        return {"ok": True}

    @app.post("/api/diaries/delivery", dependencies=protected)
    def delivery(data: DiaryDelivery):
        return call(lambda: s.periods.delivery(data.kind, data.day))

    @app.post("/api/diaries/{key}/polish", dependencies=protected)
    async def polish(key: int, data: PolishDiary):
        try:
            return await s.periods.polish(key, data)
        except LookupError:
            raise HTTPException(404, "日记不存在") from None
        except ValueError:
            raise HTTPException(422, "未能生成有效表达，事实层未改变") from None

    @app.get("/api/diaries/{key}", dependencies=protected)
    def detail(key: int):
        return call(lambda: s.diary.get(key))

    @app.put("/api/diaries/{key}", dependencies=protected)
    def edit(key: int, data: DiaryEdit):
        return call(lambda: s.diary.edit(key, data))

    @app.delete("/api/diaries/{key}", dependencies=protected)
    def delete(key: int, revision: int = Query(ge=0)):
        call(lambda: s.diary.delete(key, revision))
        return {"ok": True}

    @app.get("/api/diaries/{key}/export", dependencies=protected)
    def export(key: int):
        item = call(lambda: s.diary.get(key))
        return Response(
            json.dumps(item, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="diary-{item["day"]}-{key}.json"'
            },
        )
