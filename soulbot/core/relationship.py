"""Per-bot, per-conversation social affinity; scores never grant permissions."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from soulbot.core.social_state import StateConflict, observe_once


def affinity_label(value):
    if value < -40:
        return "谨慎相处"
    if value < 10:
        return "初识"
    if value < 40:
        return "认识"
    if value < 70:
        return "熟悉"
    return "亲近"


class RelationshipEngine:
    def __init__(self, config, db, clock=time.time):
        self.config, self.db, self.clock = config, db, clock

    @staticmethod
    def _public(row):
        data = dict(row)
        data["automatic"] = bool(data["automatic"])
        data["affinity_label"] = affinity_label(data["affinity"])
        return data

    def get(self, event):
        if not self.config.current.relationships.enabled:
            return None
        with self.db.connect() as db:
            row = db.execute(
                "SELECT * FROM relationships WHERE bot_id=? AND group_id=? AND user_id=?",
                (event.bot_id, event.group_id or "", event.user_id),
            ).fetchone()
        return self._public(row) if row else None

    def listing(self, query="", offset=0, limit=30):
        # substr matching keeps %, _ and other query text literal.
        where = "WHERE instr(user_id,?)>0 OR instr(display_name,?)>0 OR instr(group_id,?)>0"
        args = (query, query, query)
        with self.db.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM relationships " + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM relationships " + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return {
            "items": [self._public(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def create(self, bot_id, user_id, group_id, display_name, affinity, automatic):
        now = self.clock()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM relationships WHERE bot_id=? AND group_id=? AND user_id=?",
                (bot_id, group_id, user_id),
            ).fetchone():
                raise StateConflict("当前会话的用户关系已存在，请编辑原记录")
            cursor = db.execute(
                "INSERT INTO relationships (bot_id,user_id,group_id,display_name,affinity,"
                "automatic,updated_at) VALUES (?,?,?,?,?,?,?)",
                (bot_id, user_id, group_id, display_name, affinity, automatic, now),
            )
            row = db.execute(
                "SELECT * FROM relationships WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            db.execute("INSERT INTO audit(action,detail) VALUES ('relationship_created','manual')")
        return self._public(row)

    def edit(self, key, revision, *, display_name, affinity, automatic):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM relationships WHERE id=?", (key,)).fetchone()
            if not row:
                raise LookupError("这条关系已不存在，请刷新列表")
            if row["revision"] != revision:
                raise StateConflict("关系已被新的互动或编辑更新，请刷新后再保存")
            # Manual edits intentionally preserve reward cooldown and today's accumulated gain.
            db.execute(
                "UPDATE relationships SET display_name=?,affinity=?,automatic=?,updated_at=?,"
                "revision=revision+1 WHERE id=?",
                (display_name, affinity, automatic, self.clock(), key),
            )
            row = db.execute("SELECT * FROM relationships WHERE id=?", (key,)).fetchone()
            db.execute("INSERT INTO audit(action,detail) VALUES ('relationship_edited','manual')")
        return self._public(row)

    def delete(self, key, revision):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM relationships WHERE id=?", (key,)).fetchone()
            if not row:
                raise LookupError("这条关系已不存在，请刷新列表")
            if row["revision"] != revision:
                raise StateConflict("关系已更新，请刷新后再确认删除")
            db.execute("DELETE FROM relationships WHERE id=?", (key,))
            db.execute("INSERT INTO audit(action,detail) VALUES ('relationship_deleted','manual')")

    def interact(self, event):
        settings, now = self.config.current, self.clock()
        policy = settings.relationships
        if not policy.enabled:
            return
        day = datetime.fromtimestamp(now, ZoneInfo(settings.soulbot.timezone)).date().isoformat()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not observe_once(db, event, "relationship", now):
                return
            db.execute(
                "INSERT OR IGNORE INTO relationships (bot_id,user_id,group_id,updated_at) "
                "VALUES (?,?,?,?)",
                (event.bot_id, event.user_id, event.group_id or "", now),
            )
            row = db.execute(
                "SELECT * FROM relationships WHERE bot_id=? AND group_id=? AND user_id=?",
                (event.bot_id, event.group_id or "", event.user_id),
            ).fetchone()
            if not row["automatic"]:
                return
            daily_gain = row["daily_gain"] if row["reward_day"] == day else 0
            last_reward = row["last_reward_at"]
            gain = 0
            if policy.automatic and (
                last_reward is None or now - last_reward >= policy.reward_cooldown_seconds
            ):
                gain = max(
                    0,
                    min(
                        policy.affinity_per_interaction,
                        policy.max_daily_gain - daily_gain,
                        100 - row["affinity"],
                    ),
                )
            db.execute(
                "UPDATE relationships SET affinity=affinity+?, interactions=interactions+1,"
                "last_interaction_at=?,last_reward_at=?,reward_day=?,daily_gain=?,updated_at=?,"
                "revision=revision+1 WHERE id=?",
                (gain, now, now if gain else last_reward, day, daily_gain + gain, now, row["id"]),
            )
