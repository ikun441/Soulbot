"""Administrator editing APIs for simulated emotion and scoped relationships."""

from fastapi import Depends, HTTPException, Query
from pydantic import Field

from soulbot.core.emotion import STATES
from soulbot.services.config_service import EmotionValues, Model


class Revision(Model):
    revision: int = Field(ge=0)


class EmotionEdit(Revision):
    values: EmotionValues


class RelationshipFields(Model):
    display_name: str = Field(default="", max_length=40, pattern=r"^[^\x00-\x1f]*$")
    affinity: int = Field(default=0, ge=-100, le=100, strict=True)
    automatic: bool = True


class RelationshipCreate(RelationshipFields):
    bot_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    user_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    group_id: str = Field(default="", pattern=r"^([1-9][0-9]{4,11})?$")


class RelationshipEdit(RelationshipFields, Revision):
    pass


def install_social_api(app, s, authorized):
    protected = [Depends(authorized)]

    @app.get("/api/emotion", dependencies=protected)
    def emotion():
        return {"current": s.emotion.snapshot(), "history": s.emotion.history(), "states": STATES}

    @app.put("/api/emotion", dependencies=protected)
    def edit_emotion(data: EmotionEdit):
        return s.emotion.edit(data.values.model_dump(), data.revision)

    @app.post("/api/emotion/reset", dependencies=protected)
    def reset_emotion(data: Revision):
        return s.emotion.edit(
            s.config.current.emotion.baseline.model_dump(), data.revision, reset=True
        )

    @app.get("/api/relationships", dependencies=protected)
    def relationships(
        query: str = Query(default="", max_length=80),
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=30, ge=1, le=100),
    ):
        return s.relationships.listing(query, offset, limit)

    @app.post("/api/relationships", dependencies=protected)
    def create_relationship(data: RelationshipCreate):
        return s.relationships.create(**data.model_dump())

    @app.put("/api/relationships/{key}", dependencies=protected)
    def edit_relationship(key: int, data: RelationshipEdit):
        try:
            return s.relationships.edit(key, **data.model_dump())
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @app.delete("/api/relationships/{key}", dependencies=protected)
    def delete_relationship(key: int, revision: int = Query(ge=0)):
        try:
            s.relationships.delete(key, revision)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        return {"ok": True}
