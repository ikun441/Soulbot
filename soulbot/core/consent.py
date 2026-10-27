"""User-issued private-contact consent. Administrator edits can only restrict it."""

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import Field

from soulbot.core.social_state import StateConflict
from soulbot.services.config_service import Model

CLOCK_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class ContactEdit(Model):
    revision: int = Field(ge=0, strict=True)
    admin_blocked: bool = False
    quiet_start: str = Field(default="00:00", pattern=CLOCK_PATTERN)
    quiet_end: str = Field(default="08:00", pattern=CLOCK_PATTERN)


def in_quiet_window(now, timezone, start, end):
    value = datetime.fromtimestamp(now, ZoneInfo(timezone)).strftime("%H:%M")
    if start == end:
        return True
    return start <= value < end if start < end else value >= start or value < end


class ConsentStore:
    def __init__(self, config, db, clock=time.time):
        self.config, self.db, self.clock = config, db, clock

    @staticmethod
    def row(row):
        if row is None:
            return None
        item = dict(row)
        for key in ("opted_in", "user_paused", "admin_blocked"):
            item[key] = bool(item[key])
        item["source"] = "qq_private_command"
        return item

    def get(self, bot_id, user_id):
        with self.db.connect() as db:
            return self.row(
                db.execute(
                    "SELECT * FROM proactive_contacts WHERE bot_id=? AND user_id=?",
                    (bot_id, user_id),
                ).fetchone()
            )

    def listing(self, query="", offset=0, limit=30):
        with self.db.connect() as db:
            args = [query, query]
            clause = " WHERE instr(bot_id,?)>0 OR instr(user_id,?)>0"
            total = db.execute("SELECT COUNT(*) FROM proactive_contacts" + clause, args).fetchone()[
                0
            ]
            rows = db.execute(
                "SELECT * FROM proactive_contacts"
                + clause
                + " ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return {"items": [self.row(row) for row in rows], "total": total}

    def set_user(self, event, *, consent=None, paused=None):
        if event.group_id or any(
            not re.fullmatch(r"[1-9][0-9]{4,11}", value) for value in (event.bot_id, event.user_id)
        ):
            raise ValueError("主动私聊授权只能来自对应用户的 QQ 私聊")
        if (consent is None) == (paused is None):
            raise ValueError("一次只能修改一个用户偏好")
        now = self.clock()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO proactive_contacts(bot_id,user_id,updated_at) VALUES (?,?,?)",
                (event.bot_id, event.user_id, now),
            )
            if consent is not None:
                db.execute(
                    "UPDATE proactive_contacts SET opted_in=?,authorized_at=CASE WHEN ? THEN ? ELSE authorized_at END,revoked_at=?,updated_at=?,revision=revision+1 WHERE bot_id=? AND user_id=?",
                    (
                        int(consent),
                        int(consent),
                        now,
                        None if consent else now,
                        now,
                        event.bot_id,
                        event.user_id,
                    ),
                )
                if not consent:
                    self.cancel_pending(db, event.bot_id, event.user_id, now)
            else:
                db.execute(
                    "UPDATE proactive_contacts SET user_paused=?,updated_at=?,revision=revision+1 WHERE bot_id=? AND user_id=?",
                    (int(paused), now, event.bot_id, event.user_id),
                )
            row = db.execute(
                "SELECT * FROM proactive_contacts WHERE bot_id=? AND user_id=?",
                (event.bot_id, event.user_id),
            ).fetchone()
        self.db.audit("proactive_user_preference")
        return self.row(row)

    @staticmethod
    def cancel_pending(db, bot_id, user_id, now):
        jobs = db.execute(
            "SELECT id FROM proactive_jobs WHERE bot_id=? AND user_id=? AND status IN ('pending_approval','scheduled','waiting')",
            (bot_id, user_id),
        ).fetchall()
        for row in jobs:
            db.execute(
                "UPDATE proactive_jobs SET status='cancelled',reason='consent_revoked',updated_at=?,revision=revision+1 WHERE id=?",
                (now, row["id"]),
            )
            db.execute(
                "INSERT INTO proactive_job_events(job_id,status,reason,created_at) VALUES (?,'cancelled','consent_revoked',?)",
                (row["id"], now),
            )

    def edit(self, key, data: ContactEdit):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM proactive_contacts WHERE id=?", (key,)).fetchone()
            if row is None:
                raise LookupError("授权记录不存在；用户需先从 QQ 私聊设置偏好")
            if row["revision"] != data.revision:
                raise StateConflict("用户偏好已改变，请刷新后核对")
            db.execute(
                "UPDATE proactive_contacts SET admin_blocked=?,quiet_start=?,quiet_end=?,updated_at=?,revision=revision+1 WHERE id=?",
                (int(data.admin_blocked), data.quiet_start, data.quiet_end, self.clock(), key),
            )
            item = self.row(
                db.execute("SELECT * FROM proactive_contacts WHERE id=?", (key,)).fetchone()
            )
        self.db.audit("proactive_contact_edited")
        return item
