"""Local, scope-isolated diaries from confirmed activity metadata; no chat-body capture."""

import asyncio
import hashlib
import json
import time
from datetime import date, datetime, timedelta
from datetime import time as daytime
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from soulbot.core.social_state import StateConflict
from soulbot.services.config_service import Model


class GenerateDiary(Model):
    include_current_day: bool = False
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("day")
    @classmethod
    def valid_day(cls, value):
        date.fromisoformat(value)
        return value


class DiaryEdit(Model):
    revision: int = Field(ge=0, strict=True)
    title: str = Field(min_length=1, max_length=80)
    role_text: str = Field(min_length=1, max_length=4000)

    @field_validator("title", "role_text")
    @classmethod
    def clean(cls, value):
        if not value.strip() or any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
            raise ValueError("内容不能为空或包含控制字符")
        return value.strip()


class DiaryService:
    def __init__(self, config, db, clock=time.time):
        self.config, self.db, self.clock = config, db, clock
        self.task = None
        self.closed = False
        self.periods = None

    def today(self):
        return datetime.fromtimestamp(
            self.clock(), ZoneInfo(self.config.current.soulbot.timezone)
        ).date()

    def bounds(self, day, timezone=None):
        value = date.fromisoformat(day)
        zone = ZoneInfo(timezone or self.config.current.soulbot.timezone)
        return datetime.combine(value, daytime(), zone).timestamp(), datetime.combine(
            value + timedelta(days=1), daytime(), zone
        ).timestamp()

    def cleanup(self, db):
        cutoff = (
            self.today() - timedelta(days=self.config.current.diary.fact_retention_days)
        ).isoformat()
        db.execute("DELETE FROM diary_events WHERE occurred_at<?", (self.bounds(cutoff)[0],))

    def record(self, event, *, kind="chat", has_text=True, has_media=False, source_key=None):
        if kind not in {"chat", "proactive"} or (not has_text and not has_media):
            return False
        if not self.config.current.diary.enabled:
            return False
        digest = hashlib.sha256(f"{kind}:{source_key or event.key}".encode()).hexdigest()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pref = db.execute(
                "SELECT enabled FROM diary_preferences WHERE scope=?", (event.scope,)
            ).fetchone()
            if not self.config.current.diary.enabled or (pref and not pref[0]):
                return False
            self.cleanup(db)
            return bool(
                db.execute(
                    "INSERT OR IGNORE INTO diary_events VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        digest,
                        event.scope,
                        event.bot_id,
                        event.user_id,
                        event.group_id or "",
                        kind,
                        int(has_text),
                        int(has_media),
                        self.clock(),
                    ),
                ).rowcount
            )

    def preference(self, event, enabled):
        with self.db.connect() as db:
            db.execute(
                "INSERT INTO diary_preferences VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled",
                (event.scope, int(enabled)),
            )
        self.db.audit("diary_preference_changed")

    def forget(self, event):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("DELETE FROM diaries WHERE scope=?", (event.scope,)).rowcount
            db.execute("DELETE FROM diary_events WHERE scope=?", (event.scope,))
            db.execute("DELETE FROM diary_summaries WHERE scope=?", (event.scope,))
            # Deleting personal diaries also pauses future collection in this scope.
            db.execute(
                "INSERT INTO diary_preferences VALUES (?,0) ON CONFLICT(scope) DO UPDATE SET enabled=0",
                (event.scope,),
            )
        self.db.audit("diary_forgotten")
        return count

    def capture_qzone(self):
        if not self.config.current.diary.enabled:
            return
        cutoff = self.clock() - self.config.current.diary.fact_retention_days * 86400
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT id,bot_id,updated_at FROM qzone_drafts WHERE status='published' AND tid IS NOT NULL AND updated_at>=? AND id NOT IN (SELECT draft_id FROM diary_qzone_seen)",
                (cutoff,),
            ).fetchall():
                scope = "qzone:" + row["bot_id"]
                pref = db.execute(
                    "SELECT enabled FROM diary_preferences WHERE scope=?", (scope,)
                ).fetchone()
                if pref and not pref[0]:
                    continue
                db.execute("INSERT INTO diary_qzone_seen VALUES (?,?)", (row["id"], self.clock()))
                db.execute(
                    "INSERT OR IGNORE INTO diary_events VALUES (?,?,?,?,?,'qzone',1,0,?)",
                    (
                        hashlib.sha256(f"qzone:{row['id']}".encode()).hexdigest(),
                        scope,
                        row["bot_id"],
                        row["bot_id"],
                        "",
                        row["updated_at"],
                    ),
                )

    def refresh_today(self):
        if self.config.current.diary.enabled:
            self.capture_qzone()
            if self.config.current.diary.live_preview:
                return self.generate(self.today().isoformat(), include_current_day=True)

    @staticmethod
    def public(row):
        item = dict(row)
        item["facts"] = json.loads(item.pop("facts_json"))
        item["visibility"] = (
            "bot_activity"
            if item["scope"].startswith("qzone:")
            else "group"
            if item["group_id"]
            else "private"
        )
        item["is_final"] = bool(item["is_final"])
        return item

    def status(self):
        self.refresh_today()
        today = self.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        start, end = self.bounds(yesterday)
        with self.db.connect() as db:
            self.cleanup(db)
            today_count = db.execute(
                "SELECT COUNT(*) FROM diary_events WHERE occurred_at>=? AND occurred_at<?",
                self.bounds(today.isoformat()),
            ).fetchone()[0]
            yesterday_count = db.execute(
                "SELECT COUNT(*) FROM diary_events WHERE occurred_at>=? AND occurred_at<?",
                (start, end),
            ).fetchone()[0]
            eligible = db.execute(
                "SELECT COUNT(*) FROM (SELECT e.scope FROM diary_events e LEFT JOIN diary_preferences p ON e.scope=p.scope WHERE occurred_at>=? AND occurred_at<? AND COALESCE(p.enabled,1)=1 GROUP BY e.scope HAVING COUNT(*)>=?)",
                (start, end, self.config.current.diary.min_events),
            ).fetchone()[0]
            last = db.execute(
                "SELECT * FROM diary_runs ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
        return {
            "enabled": self.config.current.diary.enabled,
            "live_preview": self.config.current.diary.live_preview,
            "min_events": self.config.current.diary.min_events,
            "auto_generate": self.config.current.diary.auto_generate,
            "generation_time": self.config.current.diary.generation_time,
            "timezone": self.config.current.soulbot.timezone,
            "today": today.isoformat(),
            "yesterday": yesterday,
            "earliest_day": (
                today - timedelta(days=self.config.current.diary.fact_retention_days)
            ).isoformat(),
            "today_events": today_count,
            "yesterday_events": yesterday_count,
            "eligible_scopes": eligible,
            "last_auto_run": dict(last) if last else None,
        }

    def generate(self, day, *, automatic=False, include_current_day=False):
        target = date.fromisoformat(day)
        policy = self.config.current.diary
        if not policy.enabled:
            raise ValueError("请先启用本地日记")
        if (
            target > self.today()
            or (target == self.today() and (automatic or not include_current_day))
            or target < self.today() - timedelta(days=policy.fact_retention_days)
        ):
            raise ValueError("请选择活动保留期内已结束的自然日")
        zone = self.config.current.soulbot.timezone
        start, end = self.bounds(day, zone)
        created = existing = skipped = updated = 0
        is_final = target < self.today()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.cleanup(db)
            if automatic and db.execute("SELECT 1 FROM diary_runs WHERE day=?", (day,)).fetchone():
                return {
                    "day": day,
                    "created": 0,
                    "existing": 0,
                    "skipped_scopes": 0,
                    "already_processed": True,
                }
            groups = db.execute(
                """SELECT e.scope,e.bot_id,e.user_id,e.group_id,COUNT(*) AS total,
                SUM(CASE WHEN kind='chat' THEN 1 ELSE 0 END) AS chat_replies,
                SUM(CASE WHEN kind='chat' AND has_text=1 THEN 1 ELSE 0 END) AS text_chats,
                SUM(CASE WHEN kind='chat' AND has_media=1 THEN 1 ELSE 0 END) AS media_chats,
                SUM(CASE WHEN kind='proactive' THEN 1 ELSE 0 END) AS proactive_messages,
                SUM(CASE WHEN kind='qzone' THEN 1 ELSE 0 END) AS qzone_posts,
                MIN(occurred_at) AS first_event_at,MAX(occurred_at) AS last_event_at
                FROM diary_events e LEFT JOIN diary_preferences p ON e.scope=p.scope
                WHERE occurred_at>=? AND occurred_at<? AND COALESCE(p.enabled,1)=1 GROUP BY e.scope""",
                (start, end),
            ).fetchall()
            for row in groups:
                previous = db.execute(
                    "SELECT * FROM diaries WHERE scope=? AND day=?", (row["scope"], day)
                ).fetchone()
                if previous and previous["is_final"]:
                    existing += 1
                    continue
                if row["total"] < policy.min_events:
                    skipped += 1
                    continue
                facts = {
                    key: row[key]
                    for key in (
                        "total",
                        "chat_replies",
                        "text_chats",
                        "media_chats",
                        "proactive_messages",
                        "qzone_posts",
                        "first_event_at",
                        "last_event_at",
                    )
                }
                channel = "这段群内会话" if row["group_id"] else "这段私聊"
                name = self.config.current.soulbot.name
                title = f"{day} · {name}的{'空间活动' if row['scope'].startswith('qzone:') else '会话'}记录"[
                    :80
                ]
                role_text = f"今天在{channel}中，完成了 {facts['chat_replies']} 次有平台回执的交流，以及 {facts['proactive_messages']} 次已确认的主动联系。{name}把这些真实发生的回应，留作今天的一页小记录。"
                if row["scope"].startswith("qzone:"):
                    role_text = f"今天完成了 {facts['qzone_posts']} 次获得平台回执的空间发布。{name}把这份真实发生的分享，留在本地日记里。"
                now = self.clock()
                if previous:
                    if (
                        json.loads(previous["facts_json"]) != facts
                        or bool(previous["is_final"]) != is_final
                    ):
                        db.execute(
                            "UPDATE diaries SET facts_json=?,is_final=?,role_text=CASE WHEN role_source IN ('manual','model') THEN role_text ELSE ? END,updated_at=?,revision=revision+1 WHERE id=?",
                            (json.dumps(facts), int(is_final), role_text, now, previous["id"]),
                        )
                        updated += 1
                    else:
                        existing += 1
                    continue
                db.execute(
                    """INSERT INTO diaries(scope,bot_id,user_id,group_id,day,timezone,starts_at,ends_at,
                    title,facts_json,role_text,role_source,created_at,updated_at,is_final)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'template',?,?,?)""",
                    (
                        row["scope"],
                        row["bot_id"],
                        row["user_id"],
                        row["group_id"],
                        day,
                        zone,
                        start,
                        end,
                        title,
                        json.dumps(facts),
                        role_text,
                        now,
                        now,
                        int(is_final),
                    ),
                )
                created += 1
            if automatic:
                db.execute(
                    "INSERT INTO diary_runs VALUES (?,?,?,?,?)",
                    (day, zone, self.clock(), created, skipped),
                )
        if created or updated or not include_current_day:
            self.db.audit("diary_generated", "automatic" if automatic else "manual")
        return {
            "day": day,
            "created": created,
            "existing": existing,
            "skipped_scopes": skipped,
            "already_processed": False,
            "updated": updated,
            "is_final": is_final,
        }

    def listing(self, query="", day="", offset=0, limit=30):
        self.refresh_today()
        clause = " WHERE (instr(bot_id,?)>0 OR instr(user_id,?)>0 OR instr(group_id,?)>0 OR instr(title,?)>0)"
        args = [query] * 4
        if day:
            clause += " AND day=?"
            args.append(day)
        with self.db.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM diaries" + clause, args).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM diaries" + clause + " ORDER BY day DESC,id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return {"items": [self.public(row) for row in rows], "total": total}

    def get(self, key):
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM diaries WHERE id=?", (key,)).fetchone()
        if row is None:
            raise LookupError("日记不存在")
        return self.public(row)

    def edit(self, key, data: DiaryEdit):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM diaries WHERE id=?", (key,)).fetchone()
            if row is None:
                raise LookupError("日记不存在")
            if row[0] != data.revision:
                raise StateConflict("日记已改变，请刷新后核对")
            db.execute(
                "UPDATE diaries SET title=?,role_text=?,role_source='manual',updated_at=?,revision=revision+1 WHERE id=?",
                (data.title, data.role_text, self.clock(), key),
            )
        self.db.audit("diary_edited")
        return self.get(key)

    def delete(self, key, revision):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM diaries WHERE id=?", (key,)).fetchone()
            if row is None:
                raise LookupError("日记不存在")
            if row["revision"] != revision:
                raise StateConflict("日记已改变，请刷新后核对")
            db.execute(
                "DELETE FROM diary_events WHERE scope=? AND occurred_at>=? AND occurred_at<?",
                (row["scope"], row["starts_at"], row["ends_at"]),
            )
            db.execute("DELETE FROM diaries WHERE id=?", (key,))
            db.execute(
                "DELETE FROM diary_summaries WHERE scope=? AND week_start<=? AND week_end>?",
                (row["scope"], row["day"], row["day"]),
            )
        self.db.audit("diary_deleted")

    async def tick(self):
        await asyncio.to_thread(self.refresh_today)
        policy = self.config.current.diary
        if self.config.current.automation.paused or not policy.enabled or not policy.auto_generate:
            return
        now = datetime.fromtimestamp(self.clock(), ZoneInfo(self.config.current.soulbot.timezone))
        if now.strftime("%H:%M") >= policy.generation_time:
            await asyncio.to_thread(
                self.generate, (now.date() - timedelta(days=1)).isoformat(), automatic=True
            )
            if self.periods:
                await self.periods.tick()

    async def startup(self):
        if self.task and not self.task.done():
            return
        self.closed = False

        async def loop():
            while not self.closed:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.to_thread(self.db.audit, "diary_task_failed")
                await asyncio.sleep(60)

        self.task = asyncio.create_task(loop(), name="soulbot-local-diary")

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
